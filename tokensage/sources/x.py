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
    quoted_tweet_id: str | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    views: int | None = None
    community: dict[str, Any] | None = None
    possibly_sensitive: bool | None = None
    # The tweet this one quotes, when the source returns it inline (one level only).
    quoted: TweetData | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("raw", "quoted")}
        for k in ("created_at", "author_joined"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        if self.quoted is not None:
            d["quoted"] = self.quoted.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TweetData:
        d = dict(d)
        for k in ("created_at", "author_joined"):
            if d.get(k):
                d[k] = datetime.fromisoformat(d[k])
        q = d.get("quoted")
        d["quoted"] = cls.from_json(q) if isinstance(q, dict) else None
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
            d["joined"] = datetime.fromisoformat(d["joined"])
        d.setdefault("raw", {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ----------------------------------------------------------------- helpers


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, int | float):
        return datetime.fromtimestamp(v if v < 1e11 else v / 1000, tz=UTC)
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).astimezone(UTC)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


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
        quoted_tweet_id=str(quote.get("id")) if quote.get("id") else None,
        likes=_int(st.get("likes")),
        replies=_int(st.get("replies")),
        reposts=_int(st.get("reposts")),
        views=_int(st.get("views")),
        community=st.get("community") if isinstance(st.get("community"), dict) else None,
        possibly_sensitive=st.get("possibly_sensitive"),
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
        quoted_tweet_id=(j.get("qrt") or {}).get("tweetID")
        if isinstance(j.get("qrt"), dict)
        else None,
        likes=_int(j.get("likes")),
        replies=_int(j.get("replies")),
        reposts=_int(j.get("retweets")),
        possibly_sensitive=j.get("possibly_sensitive"),
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
        quoted_tweet_id=q.get("id_str"),
        likes=_int(j.get("favorite_count")),
        replies=_int(j.get("conversation_count")),
        possibly_sensitive=j.get("possibly_sensitive"),
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
    tweets = j.get("tweets") or []
    if not tweets:
        return TweetData(id=tweet_id, status="deleted", source="twitterapi_io")
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
        raw=t,
    )


async def fetch_tweet(
    http: httpx.AsyncClient, tweet_id: str, paid_key: str = "", allow_paid: bool = False
) -> TweetData:
    """Walk the chain; first definitive answer wins (ok or deleted)."""
    deleted: TweetData | None = None
    for fn in (fx_tweet, vx_tweet, syndication_tweet, oembed_tweet):
        try:
            t = await fn(http, tweet_id)
        except Exception as e:  # noqa: BLE001 - one broken mirror must not stop the chain
            log.info("x.fetch_tweet.error", fn=fn.__name__, error=str(e)[:120])
            t = None
        if t is not None:
            # a deleted verdict from a mirror can be stale; keep walking for an "ok"
            if t.status == "ok":
                return t
            deleted = t
            continue
    if allow_paid and paid_key:
        t = await paid_tweet(http, tweet_id, paid_key)
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
        raw=u,
    )
    if p.username_changes is None:
        r2, _ = await _get(http, "x.fxtwitter", f"{FX}/profile/{handle}/about")
        if r2 is not None and r2.status_code == 200:
            try:
                a = r2.json()
                acc = a.get("about_account") or a.get("about") or a
                p.username_changes = _int(((acc or {}).get("username_changes") or {}).get("count"))
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
        raw=j,
    )


async def fetch_profile(http: httpx.AsyncClient, handle: str) -> ProfileData:
    for fn in (fx_profile, vx_profile):
        try:
            p = await fn(http, handle)
        except Exception as e:  # noqa: BLE001
            log.info("x.fetch_profile.error", fn=fn.__name__, error=str(e)[:120])
            p = None
        if p is not None and p.status in ("ok", "suspended"):
            return p
    return ProfileData(handle=handle, status="failed")
