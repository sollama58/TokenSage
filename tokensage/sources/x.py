"""X/Twitter content via the free fallback chain (guide §4.3), plus the optional paid tier.

Order: FxTwitter -> vxTwitter -> X syndication CDN -> oEmbed -> (paid) twitterapi.io.
Each fetcher returns a normalised TweetData/ProfileData or None and never raises; each
source has its own circuit breaker. The handle in a status URL is NEVER trusted: callers
compare it with the fetched author.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from tokensage.engine.xref import syndication_token
from tokensage.net.breaker import breaker

log = structlog.get_logger("x")

FX = "https://api.fxtwitter.com/2"
VX = "https://api.vxtwitter.com"
SYND = "https://cdn.syndication.twimg.com/tweet-result"
OEMBED = "https://publish.x.com/oembed"
PAID = "https://api.twitterapi.io/twitter"


@dataclass
class TweetData:
    id: str
    status: str  # ok | deleted | suspended | not_found | failed
    source: str | None = None
    text: str | None = None
    created_at: datetime | None = None
    author_handle: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    followers: int | None = None
    verified_type: str | None = None  # blue | business | government | legacy | None
    author_joined: datetime | None = None
    media_count: int = 0
    # photo URLs and video thumbnails (https), in post order; compared with the token logo
    media_urls: list[str] = field(default_factory=list)
    quoted_tweet_id: str | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    views: int | None = None
    community: dict[str, Any] | None = None
    possibly_sensitive: bool | None = None
    # The tweet this one quotes, when the source returns it inline (one level only).
    quoted: TweetData | None = None
    # The tweet this one replies to: its id and author from the source, the tweet itself
    # inline (syndication) or fetched separately (one level only).
    replying_to_id: str | None = None
    replying_to_handle: str | None = None
    replied_to: TweetData | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("raw", "quoted", "replied_to")}
        for k in ("created_at", "author_joined"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        if self.quoted is not None:
            d["quoted"] = self.quoted.to_json()
        if self.replied_to is not None:
            d["replied_to"] = self.replied_to.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TweetData:
        d = dict(d)
        for k in ("created_at", "author_joined"):
            if d.get(k):
                d[k] = _utc(datetime.fromisoformat(d[k]))
        q = d.get("quoted")
        d["quoted"] = cls.from_json(q) if isinstance(q, dict) else None
        rt = d.get("replied_to")
        d["replied_to"] = cls.from_json(rt) if isinstance(rt, dict) else None
        d.setdefault("raw", {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ProfileData:
    handle: str
    status: str  # ok | suspended | not_found | failed
    source: str | None = None
    user_id: str | None = None
    name: str | None = None
    followers: int | None = None
    following: int | None = None
    statuses: int | None = None
    joined: datetime | None = None
    verified_type: str | None = None
    username_changes: int | None = None
    description: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        if d.get("joined") is not None:
            d["joined"] = d["joined"].isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ProfileData:
        d = dict(d)
        if d.get("joined"):
            d["joined"] = _utc(datetime.fromisoformat(d["joined"]))
        d.setdefault("raw", {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ----------------------------------------------------------------- helpers


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        try:
            return datetime.fromtimestamp(v if v < 1e11 else v / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):  # absurd or NaN timestamps
            return None
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return _utc(datetime.strptime(s, fmt))
        except ValueError:
            pass
    try:
        return _utc(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(d: datetime) -> datetime:
    """A literal 'Z' parses as naive: that is UTC, not the server's local time."""
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _verified(v: Any) -> str | None:
    """Map the various verification shapes to blue | business | government | legacy | None."""
    if isinstance(v, dict):
        t = (v.get("type") or "").lower()
        if t in ("business", "organization"):
            return "business"
        if t == "government":
            return "government"
        if v.get("verified"):
            return "blue"
        return None
    if isinstance(v, str):
        return {"business": "business", "government": "government"}.get(v.lower())
    return None


MAX_MEDIA = 4


def _id(v: Any) -> str | None:
    """A tweet id as a string of digits, or None."""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    return s if s.isdigit() else None


def _handle(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    h = v.strip().lstrip("@")
    return h if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) else None


def _fx_reply(st: dict[str, Any]) -> tuple[str | None, str | None]:
    """FxTwitter: v2 gives replying_to {screen_name, post}; v1 gives replying_to (handle)
    and replying_to_status (id)."""
    rt = st.get("replying_to")
    if isinstance(rt, dict):
        return _id(rt.get("post") or rt.get("status")), _handle(rt.get("screen_name"))
    return _id(st.get("replying_to_status")), _handle(rt)


def _https(u: Any) -> str | None:
    return u if isinstance(u, str) and u.startswith("https://") and len(u) <= 2048 else None


def _uniq(urls: list[str | None]) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u and u not in out:
            out.append(u)
    return out[:MAX_MEDIA]


def _fx_media(st: dict[str, Any]) -> list[str]:
    media = st.get("media") or {}
    urls: list[str | None] = [_https(p.get("url")) for p in media.get("photos") or []]
    urls += [_https(v.get("thumbnail_url")) for v in media.get("videos") or []]
    for item in media.get("all") or []:
        urls.append(
            _https(item.get("thumbnail_url") if item.get("type") != "photo" else item.get("url"))
        )
    return _uniq(urls)


def _vx_media(j: dict[str, Any]) -> list[str]:
    urls: list[str | None] = []
    for m in j.get("media_extended") or []:
        if m.get("type") == "image":
            urls.append(_https(m.get("url")))
        else:
            urls.append(_https(m.get("thumbnail_url")))
    if not urls:
        urls = [
            _https(u)
            for u in j.get("mediaURLs") or []
            if str(u).split("?")[0].endswith((".jpg", ".png", ".jpeg", ".webp"))
        ]
    return _uniq(urls)


def _synd_media(j: dict[str, Any]) -> list[str]:
    return _uniq([_https(m.get("media_url_https")) for m in j.get("mediaDetails") or []])


async def _get(
    http: httpx.AsyncClient, source: str, url: str, **kw: Any
) -> tuple[httpx.Response | None, str | None]:
    if not breaker.allow(source):
        return None, "circuit_open"
    try:
        r = await http.get(url, timeout=8.0, **kw)
    except httpx.HTTPError as e:
        breaker.failure(source)
        return None, f"{type(e).__name__}"
    if r.status_code in (429,) or r.status_code >= 500:
        breaker.failure(source)
        return None, f"http_{r.status_code}"
    breaker.success(source)
    return r, None


# ----------------------------------------------------------------- tweets


async def fx_tweet(http: httpx.AsyncClient, tweet_id: str) -> TweetData | None:
    r, err = await _get(http, "x.fxtwitter", f"{FX}/status/{tweet_id}")
    if r is None:
        return None
    if r.status_code == 404:
        return TweetData(id=tweet_id, status="deleted", source="fxtwitter")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    st = j.get("status") or j.get("tweet")
    if not isinstance(st, dict):
        if j.get("code") == 404:
            return TweetData(id=tweet_id, status="deleted", source="fxtwitter")
        return None
    t = _fx_status(st, tweet_id)
    quote = st.get("quote")
    if isinstance(quote, dict) and quote.get("text"):
        t.quoted = _fx_status(quote, str(quote.get("id") or ""))
    return t


def _fx_status(st: dict[str, Any], tweet_id: str) -> TweetData:
    au = st.get("author") or {}
    media = st.get("media") or {}
    quote = st.get("quote") or {}
    return TweetData(
        id=str(st.get("id") or tweet_id),
        status="ok",
        source="fxtwitter",
        text=st.get("text"),
        created_at=_dt(st.get("created_timestamp") or st.get("created_at")),
        author_handle=au.get("screen_name"),
        author_id=str(au["id"]) if au.get("id") else None,
        author_name=au.get("name"),
        followers=_int(au.get("followers")),
        verified_type=_verified(au.get("verification")),
        author_joined=_dt(au.get("joined")),
        media_count=len(media.get("photos") or []) + len(media.get("videos") or []),
        media_urls=_fx_media(st),
        quoted_tweet_id=str(quote.get("id")) if quote.get("id") else None,
        likes=_int(st.get("likes")),
        replies=_int(st.get("replies")),
        reposts=_int(st.get("reposts")),
        views=_int(st.get("views")),
        community=st.get("community") if isinstance(st.get("community"), dict) else None,
        possibly_sensitive=st.get("possibly_sensitive"),
        replying_to_id=_fx_reply(st)[0],
        replying_to_handle=_fx_reply(st)[1],
        raw=st,
    )


async def vx_tweet(http: httpx.AsyncClient, tweet_id: str) -> TweetData | None:
    r, err = await _get(http, "x.vxtwitter", f"{VX}/i/status/{tweet_id}")
    if r is None:
        return None
    if r.status_code == 404:
        return TweetData(id=tweet_id, status="deleted", source="vxtwitter")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    if not j.get("tweetID") and not j.get("text"):
        return None
    t = _vx_status(j, tweet_id)
    qrt = j.get("qrt")
    if isinstance(qrt, dict) and qrt.get("text"):
        t.quoted = _vx_status(qrt, str(qrt.get("tweetID") or ""))
    return t


def _vx_status(j: dict[str, Any], tweet_id: str) -> TweetData:
    return TweetData(
        id=str(j.get("tweetID") or tweet_id),
        status="ok",
        source="vxtwitter",
        text=j.get("text"),
        created_at=_dt(j.get("date_epoch") or j.get("date")),
        author_handle=j.get("user_screen_name"),
        author_name=j.get("user_name"),
        media_count=len(j.get("mediaURLs") or []),
        media_urls=_vx_media(j),
        quoted_tweet_id=(j.get("qrt") or {}).get("tweetID")
        if isinstance(j.get("qrt"), dict)
        else None,
        likes=_int(j.get("likes")),
        replies=_int(j.get("replies")),
        reposts=_int(j.get("retweets")),
        possibly_sensitive=j.get("possibly_sensitive"),
        replying_to_id=_id(j.get("replyingToID")),
        replying_to_handle=_handle(j.get("replyingTo")),
        raw=j,
    )


async def syndication_tweet(http: httpx.AsyncClient, tweet_id: str) -> TweetData | None:
    url = f"{SYND}?id={tweet_id}&lang=en&token={syndication_token(tweet_id)}"
    r, err = await _get(http, "x.syndication", url)
    if r is None or r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    if not isinstance(j, dict) or not j:
        return None
    if j.get("__typename") == "TweetTombstone":
        return TweetData(id=tweet_id, status="deleted", source="syndication")
    if j.get("__typename") not in ("Tweet", None) or not j.get("text"):
        return None
    t = _synd_status(j, tweet_id)
    q = j.get("quoted_tweet")
    if isinstance(q, dict) and q.get("text"):
        t.quoted = _synd_status(q, str(q.get("id_str") or ""))
    parent = j.get("parent")
    if isinstance(parent, dict) and parent.get("text"):
        t.replied_to = _synd_status(parent, str(parent.get("id_str") or ""))
        t.replying_to_id = t.replying_to_id or t.replied_to.id or None
        t.replying_to_handle = t.replying_to_handle or t.replied_to.author_handle
    return t


def _synd_status(j: dict[str, Any], tweet_id: str) -> TweetData:
    u = j.get("user") or {}
    vt = None
    if u.get("verified_type"):
        vt = _verified(str(u["verified_type"]))
    elif u.get("is_blue_verified"):
        vt = "blue"
    elif u.get("verified"):
        vt = "legacy"
    q = j.get("quoted_tweet") or {}
    return TweetData(
        id=str(j.get("id_str") or tweet_id),
        status="ok",
        source="syndication",
        text=j.get("text"),
        created_at=_dt(j.get("created_at")),
        author_handle=u.get("screen_name"),
        author_id=u.get("id_str"),
        author_name=u.get("name"),
        verified_type=vt,
        media_count=len(j.get("mediaDetails") or []),
        media_urls=_synd_media(j),
        quoted_tweet_id=q.get("id_str"),
        likes=_int(j.get("favorite_count")),
        replies=_int(j.get("conversation_count")),
        possibly_sensitive=j.get("possibly_sensitive"),
        replying_to_id=_id(j.get("in_reply_to_status_id_str")),
        replying_to_handle=_handle(j.get("in_reply_to_screen_name")),
        raw=j,
    )


_OEMBED_P = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAGS = re.compile(r"<[^>]+>")


async def oembed_tweet(http: httpx.AsyncClient, tweet_id: str) -> TweetData | None:
    r, err = await _get(
        http,
        "x.oembed",
        OEMBED,
        params={"url": f"https://x.com/i/status/{tweet_id}", "omit_script": "1", "dnt": "true"},
    )
    if r is None:
        return None
    if r.status_code == 404:
        return TweetData(id=tweet_id, status="deleted", source="oembed")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    body = j.get("html") or ""
    m = _OEMBED_P.search(body)
    text = html.unescape(_TAGS.sub("", m.group(1))).strip() if m else None
    handle = None
    au = j.get("author_url") or ""
    if au:
        handle = au.rstrip("/").split("/")[-1] or None
    if not text and not handle:
        return None
    return TweetData(
        id=tweet_id,
        status="ok",
        source="oembed",
        text=text,
        author_handle=handle,
        author_name=j.get("author_name"),
        raw=j,
    )


async def paid_tweet(http: httpx.AsyncClient, tweet_id: str, api_key: str) -> TweetData | None:
    if not api_key:
        return None
    r, err = await _get(
        http,
        "x.twitterapi_io",
        f"{PAID}/tweets",
        params={"tweet_ids": tweet_id},
        headers={"X-API-Key": api_key},
    )
    if r is None or r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    tweets = j.get("tweets")
    if not tweets:
        # only a successful lookup with no tweet means deleted; an error body (credits,
        # auth, ...) must not be cached as a deleted post
        if j.get("status") == "success" and isinstance(tweets, list):
            return TweetData(id=tweet_id, status="deleted", source="twitterapi_io")
        return None
    if not isinstance(tweets, list) or not isinstance(tweets[0], dict):
        return None
    out = _paid_status(tweets[0], tweet_id)
    q = tweets[0].get("quoted_tweet")
    if isinstance(q, dict) and q.get("text"):
        out.quoted = _paid_status(q, str(q.get("id") or ""))
        out.quoted_tweet_id = out.quoted_tweet_id or out.quoted.id or None
    return out


def _paid_status(t: dict[str, Any], tweet_id: str) -> TweetData:
    au = t.get("author") or {}
    return TweetData(
        id=str(t.get("id") or tweet_id),
        status="ok",
        source="twitterapi_io",
        text=t.get("text"),
        created_at=_dt(t.get("createdAt")),
        author_handle=au.get("userName"),
        author_id=str(au.get("id")) if au.get("id") else None,
        author_name=au.get("name"),
        followers=_int(au.get("followers")),
        verified_type="blue" if au.get("isBlueVerified") else None,
        author_joined=_dt(au.get("createdAt")),
        likes=_int(t.get("likeCount")),
        replies=_int(t.get("replyCount")),
        reposts=_int(t.get("retweetCount")),
        views=_int(t.get("viewCount")),
        replying_to_id=_id(t.get("inReplyToId")),
        replying_to_handle=_handle(t.get("inReplyToUsername")),
        raw=t,
    )


async def _supplement_links(http: httpx.AsyncClient, t: TweetData) -> None:
    """A mirror that shows neither a reply nor a quote may simply not report them (its
    format changes, or it drops replies). X's syndication CDN reports both, with the
    parent and quoted tweets inline, so ask it and fill in what the mirror missed."""
    if t.replying_to_id or t.quoted_tweet_id or t.quoted is not None:
        return
    try:
        s = await syndication_tweet(http, t.id)
    except Exception as e:  # noqa: BLE001 - only a supplement
        log.info("x.supplement.error", error=str(e)[:120])
        return
    if s is None or s.status != "ok":
        return
    t.replying_to_id = s.replying_to_id
    t.replying_to_handle = s.replying_to_handle
    t.replied_to = s.replied_to
    t.quoted_tweet_id = s.quoted_tweet_id
    t.quoted = s.quoted
    if s.media_urls and not t.media_urls:
        t.media_urls = s.media_urls


async def fetch_tweet(
    http: httpx.AsyncClient, tweet_id: str, paid_key: str = "", allow_paid: bool = False
) -> TweetData:
    """Walk the chain; first definitive answer wins (ok or deleted)."""
    deleted: TweetData | None = None
    asked_syndication = False
    for fn in (fx_tweet, vx_tweet, syndication_tweet, oembed_tweet):
        try:
            t = await fn(http, tweet_id)
        except Exception as e:  # noqa: BLE001 - one broken mirror must not stop the chain
            log.info("x.fetch_tweet.error", fn=fn.__name__, error=str(e)[:120])
            t = None
        asked_syndication = asked_syndication or fn is syndication_tweet
        if t is not None:
            # a deleted verdict from a mirror can be stale; keep walking for an "ok"
            if t.status == "ok":
                # the syndication CDN was already asked in this walk: no second request
                if not asked_syndication:
                    await _supplement_links(http, t)
                return t
            deleted = t
            continue
    if allow_paid and paid_key:
        try:
            t = await paid_tweet(http, tweet_id, paid_key)
        except Exception as e:  # noqa: BLE001 - the paid fallback must not fail the analysis
            log.info("x.fetch_tweet.error", fn="paid_tweet", error=str(e)[:120])
            t = None
        if t is not None:
            return t
    return deleted if deleted is not None else TweetData(id=tweet_id, status="failed")


# ----------------------------------------------------------------- profiles


async def fx_profile(http: httpx.AsyncClient, handle: str) -> ProfileData | None:
    r, err = await _get(http, "x.fxtwitter", f"{FX}/profile/{handle}")
    if r is None:
        return None
    if r.status_code == 404:
        return ProfileData(handle=handle, status="not_found", source="fxtwitter")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    if j.get("reason") == "suspended":
        return ProfileData(handle=handle, status="suspended", source="fxtwitter")
    u = j.get("user")
    if not isinstance(u, dict):
        return None
    about = u.get("about_account") or {}
    changes = (about.get("username_changes") or {}).get("count")
    p = ProfileData(
        handle=u.get("screen_name") or handle,
        status="ok",
        source="fxtwitter",
        user_id=str(u["id"]) if u.get("id") else None,
        name=u.get("name"),
        followers=_int(u.get("followers")),
        following=_int(u.get("following")),
        statuses=_int(u.get("statuses")),
        joined=_dt(u.get("joined")),
        verified_type=_verified(u.get("verification")),
        username_changes=_int(changes),
        description=u.get("description"),
        avatar_url=_https(u.get("avatar_url")),
        banner_url=_https(u.get("banner_url")),
        raw=u,
    )
    if p.username_changes is None:
        r2, _ = await _get(http, "x.fxtwitter", f"{FX}/profile/{handle}/about")
        if r2 is not None and r2.status_code == 200:
            try:
                a = r2.json()
                acc = (a.get("about_account") or a.get("about") or a) if isinstance(a, dict) else {}
                uc = acc.get("username_changes") if isinstance(acc, dict) else None
                p.username_changes = _int(uc.get("count")) if isinstance(uc, dict) else None
            except ValueError:
                pass
    return p


async def vx_profile(http: httpx.AsyncClient, handle: str) -> ProfileData | None:
    r, err = await _get(http, "x.vxtwitter", f"{VX}/{handle}")
    if r is None:
        return None
    if r.status_code == 404:
        return ProfileData(handle=handle, status="not_found", source="vxtwitter")
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    if not j.get("screen_name") and not j.get("followers_count"):
        return None
    return ProfileData(
        handle=j.get("screen_name") or handle,
        status="ok",
        source="vxtwitter",
        user_id=str(j["id"]) if j.get("id") else None,
        name=j.get("name"),
        followers=_int(j.get("followers_count")),
        following=_int(j.get("following_count")),
        statuses=_int(j.get("tweet_count")),
        joined=_dt(j.get("created_at")),
        description=j.get("description"),
        avatar_url=_https(j.get("profile_image_url") or j.get("profile_image_url_https")),
        banner_url=_https(j.get("profile_banner_url")),
        raw=j,
    )


async def fetch_profile(http: httpx.AsyncClient, handle: str) -> ProfileData:
    missing: ProfileData | None = None
    for fn in (fx_profile, vx_profile):
        try:
            p = await fn(http, handle)
        except Exception as e:  # noqa: BLE001
            log.info("x.fetch_profile.error", fn=fn.__name__, error=str(e)[:120])
            p = None
        if p is not None and p.status in ("ok", "suspended"):
            return p
        if p is not None and p.status == "not_found":
            missing = p
    if missing is not None:
        return missing  # no mirror found the account and at least one says it does not exist
    return ProfileData(handle=handle, status="failed")
