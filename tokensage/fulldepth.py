"""Full-depth helpers for the analyzer: cached X content, cached OCR, the trend index and
news confirmation (guide §4.3, §5.8, §6.3). All database-aware; the engine stays pure."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage.api.schemas import XInfo
from tokensage.config import Settings
from tokensage.engine import ocr, trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.sources import gnews
from tokensage.sources.x import ProfileData, TweetData, fetch_profile, fetch_tweet

log = structlog.get_logger("fulldepth")

PROFILE_TTL = timedelta(hours=12)
TWEET_RECHECK = timedelta(hours=6)
DELETED_RETRY = timedelta(hours=1)
NEWS_TTL = timedelta(hours=1)
TREND_INDEX_TTL_S = 600
MAX_NEWS_LOOKUPS = 2


# ----------------------------------------------------------------- X content with caching


PAID_X_USAGE_KEY = "_paid_x"  # api_usage row counting paid X calls per UTC day


async def _paid_x_allowed(conn: asyncpg.Connection, settings: Settings) -> bool:
    """ENABLE_PAID_X, and today's estimated spend still under PAID_X_DAILY_USD_CAP."""
    if not (settings.enable_paid_x and settings.twitterapi_io_key):
        return False
    from tokensage.api import usage

    used = (await usage.today(conn, PAID_X_USAGE_KEY)).requests
    return used * settings.paid_x_usd_per_call < settings.paid_x_daily_usd_cap


async def tweet_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, tweet_id: str
) -> TweetData:
    """The tweet, cached. The first good copy is the record (tweets are immutable apart from
    edits), but it is re-checked every TWEET_RECHECK so a deletion is noticed, and a sparse
    oEmbed copy is upgraded when a richer source answers."""
    row = await conn.fetchrow(
        "select first_snapshot, latest, status, source, fetched_at from x_tweet where tweet_id=$1",
        tweet_id,
    )
    record: TweetData | None = None
    if row:
        age = datetime.now(UTC) - row["fetched_at"] if row["fetched_at"] else None
        if row["status"] == "ok" and row["first_snapshot"]:
            record = TweetData.from_json(row["first_snapshot"])
            if age is not None and age < TWEET_RECHECK and row["source"] != "oembed":
                return record
        elif row["status"] in ("deleted", "failed") and age is not None and age < DELETED_RETRY:
            return TweetData(id=tweet_id, status=row["status"], source=row["source"])
    paid_ok = await _paid_x_allowed(conn, settings)
    t = await fetch_tweet(http, tweet_id, paid_key=settings.twitterapi_io_key, allow_paid=paid_ok)
    if t.source == "twitterapi_io":
        from tokensage.api import usage

        await usage.bump(conn, PAID_X_USAGE_KEY, requests=1)
    snap = t.to_json()
    await conn.execute(
        """insert into x_tweet (tweet_id, first_snapshot, latest, status, source, fetched_at)
           values ($1, $2, $2, $3, $4, now())
           on conflict (tweet_id) do update set
             latest = case when excluded.status = 'failed' then x_tweet.latest
                           else excluded.latest end,
             first_snapshot = case
               when x_tweet.status <> 'ok' then excluded.first_snapshot
               when x_tweet.source = 'oembed' and excluded.status = 'ok'
                    and excluded.source <> 'oembed' then excluded.first_snapshot
               else x_tweet.first_snapshot end,
             -- a failed re-check (mirrors down) changes nothing; 'deleted' is a real verdict
             status = case when excluded.status = 'failed' and x_tweet.status = 'ok'
                           then x_tweet.status else excluded.status end,
             source = case when excluded.status = 'failed' and x_tweet.status = 'ok'
                           then x_tweet.source else excluded.source end,
             fetched_at = now()""",
        tweet_id,
        snap,
        t.status,
        t.source,
    )
    if record is not None:
        if t.status == "deleted":
            return t
        if t.status == "ok" and row is not None and row["source"] == "oembed":
            return t  # the richer copy replaces the sparse oEmbed record
        if t.status == "ok":
            _fill_missing(record, t)
        return record
    return t


# Fields older cache records may lack: filled from a fresh copy without replacing the record.
_BACKFILL = ("media_urls", "quoted_tweet_id", "quoted", "replying_to_id", "replying_to_handle")


def _fill_missing(record: TweetData, fresh: TweetData) -> None:
    for f in (*_BACKFILL, "replied_to"):
        if not getattr(record, f) and getattr(fresh, f):
            setattr(record, f, getattr(fresh, f))


async def profile_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, handle: str
) -> ProfileData:
    row = await conn.fetchrow(
        """select user_id, handle, snapshot, source, fetched_at from x_profile
           where lower(handle) = lower($1) order by fetched_at desc limit 1""",
        handle,
    )
    if row and row["snapshot"] and row["fetched_at"]:
        if datetime.now(UTC) - row["fetched_at"] < PROFILE_TTL:
            return ProfileData.from_json(row["snapshot"])
    p = await fetch_profile(http, handle)
    if p.status == "failed":
        if row and row["snapshot"]:
            return ProfileData.from_json(row["snapshot"])  # stale beats nothing
        return p
    key = p.user_id or f"handle:{handle.lower()}"
    prev = await conn.fetchrow("select handle, snapshot from x_profile where user_id=$1", key)
    # Work out renames before saving, so the stored snapshot (served from cache for 12 h)
    # carries the same count as this response. A count from the source already includes
    # the change; only our own observation is added when the source gave none.
    prev_changes = (prev["snapshot"] or {}).get("username_changes") if prev else None
    renamed = bool(
        prev and prev["handle"] and p.handle and prev["handle"].lower() != p.handle.lower()
    )
    if p.username_changes is None:
        base = prev_changes if isinstance(prev_changes, int) else 0
        p.username_changes = base + (1 if renamed else 0) or None
    if renamed:
        log.info("x.profile.renamed", user_id=key, old=prev["handle"], new=p.handle)
    await conn.execute(
        """insert into x_profile (user_id, handle, snapshot, status, source, fetched_at)
           values ($1, $2, $3, $4, $5, now())
           on conflict (user_id) do update set handle=excluded.handle, snapshot=excluded.snapshot,
             status=excluded.status, source=excluded.source, fetched_at=now()""",
        key,
        p.handle,
        p.to_json(),
        p.status,
        p.source,
    )
    await conn.execute(
        "insert into x_profile_history (user_id, handle, followers) values ($1, $2, $3)",
        key,
        p.handle,
        p.followers,
    )
    return p


async def x_content(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, x: XInfo | None
) -> tuple[TweetData | None, ProfileData | None]:
    if x is None:
        return None, None
    tweet: TweetData | None = None
    profile: ProfileData | None = None
    if x.ref.kind == "tweet" and x.ref.tweet_id:
        tweet = await tweet_cached(conn, http, settings, x.ref.tweet_id)
        if (
            tweet.status == "ok"
            and tweet.quoted is None
            and tweet.quoted_tweet_id
            and tweet.quoted_tweet_id != tweet.id
        ):
            # oEmbed and pre-quote cache rows carry only the quoted id: fetch it (cached too)
            tweet.quoted = await tweet_cached(conn, http, settings, tweet.quoted_tweet_id)
        if (
            tweet.status == "ok"
            and tweet.replied_to is None
            and tweet.replying_to_id
            and tweet.replying_to_id != tweet.id
        ):
            # a reply: the post it answers is usually the context the coin is about
            tweet.replied_to = await tweet_cached(conn, http, settings, tweet.replying_to_id)
        if tweet.status == "ok" and tweet.author_handle:
            profile = await profile_cached(conn, http, tweet.author_handle)
    elif x.ref.kind == "profile" and x.ref.handle:
        profile = await profile_cached(conn, http, x.ref.handle)
    return tweet, profile


# ----------------------------------------------------------------- OCR cache


async def ocr_cached(conn: asyncpg.Connection, content_key: str | None) -> list[ocr.OcrLine] | None:
    if not content_key:
        return None
    row = await conn.fetchrow("select ocr, ocr_conf from image where content_key=$1", content_key)
    if not row or row["ocr"] is None:
        return None
    confs = list(row["ocr_conf"] or [])
    return [
        ocr.OcrLine(t, float(confs[i]) if i < len(confs) else 0.0)
        for i, t in enumerate(row["ocr"] or [])
    ]


async def persist_ocr(conn: asyncpg.Connection, content_key: str, lines: list[ocr.OcrLine]) -> None:
    await conn.execute(
        "update image set ocr=$2, ocr_conf=$3 where content_key=$1",
        content_key,
        [ln.text for ln in lines],
        [ln.confidence for ln in lines],
    )


# ----------------------------------------------------------------- trend index


_trend_cache: tuple[float, trends.TrendIndex] | None = None


async def trend_index(conn: asyncpg.Connection) -> trends.TrendIndex:
    global _trend_cache
    now = time.monotonic()
    if _trend_cache and now - _trend_cache[0] < TREND_INDEX_TTL_S:
        return _trend_cache[1]
    rows = await conn.fetch(
        """with latest as (select max(day) d from trend_term where source='wikipedia'),
           best as (
             -- one row per term (its strongest day): duplicates across days would otherwise
             -- overwrite each other in the index, last (weakest) wins
             select distinct on (term) term, spike, views from trend_term, latest
             where source='wikipedia' and day >= latest.d - 2
               and (spike >= 2 or views >= 150000)
             order by term, spike desc nulls last)
           select term, spike, views from best
           order by spike desc nulls last limit 1500"""
    )
    terms = [
        trends.TrendTerm(r["term"], float(r["spike"] or 1.0), int(r["views"] or 0)) for r in rows
    ]
    idx = trends.TrendIndex(terms, load_knowledge())
    _trend_cache = (now, idx)
    return idx


# ----------------------------------------------------------------- news confirmation


async def news_for(
    conn: asyncpg.Connection, http: httpx.AsyncClient, term: str
) -> list[dict[str, Any]] | None:
    key = f"gnews:{term.lower()}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and datetime.now(UTC) - row["fetched_at"] < NEWS_TTL:
        return list(row["value"])
    heads = await gnews.search(http, term)
    if heads is None:
        return list(row["value"]) if row else None
    value = [{"title": h.title, "source": h.source, "published": h.published} for h in heads]
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        value,
    )
    return value


# ----------------------------------------------------------------- X media hashes

MEDIA_FAILED_RETRY = timedelta(hours=6)


def media_urls(tweet: TweetData | None, profile: ProfileData | None) -> list[str]:
    """The images to compare with the token logo: the post's photos and video thumbnails
    (then the quoted and replied-to posts'), or a linked profile's avatar and banner. At most 4."""
    from tokensage.sources.x import MAX_MEDIA

    urls: list[str] = []
    if tweet is not None and tweet.status == "ok":
        urls += tweet.media_urls
        for other in (tweet.quoted, tweet.replied_to):
            if other is not None and other.status == "ok":
                urls += other.media_urls
    elif profile is not None and profile.status == "ok":
        urls += [u for u in (profile.avatar_url, profile.banner_url) if u]
    seen: list[str] = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    return seen[:MAX_MEDIA]


def _small(url: str) -> str:
    """X serves several sizes; the small one is plenty for a perceptual hash."""
    if url.startswith("https://pbs.twimg.com/media/") and "name=" not in url:
        return url + ("&" if "?" in url else "?") + "name=small"
    if "pbs.twimg.com/profile_images/" in url:
        return url.replace("_normal.", "_400x400.")
    return url


async def media_hashes(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, urls: list[str]
) -> list[Any]:
    """Fetch (SSRF-guarded, same size/time caps as logos) and hash each media URL. Cached by
    URL: hashes forever, failures retried after 6 h."""
    import asyncio

    from tokensage.engine import image as image_stage
    from tokensage.engine.xmatch import MediaHash
    from tokensage.net.safe_fetch import FetchError, UnsafeUrl
    from tokensage.resolve.metadata import fetch_url

    cached: dict[str, MediaHash] = {}
    for url in urls:
        row = await conn.fetchrow("select * from x_media where url=$1", url)
        if row and (
            row["status"] == "ok" or datetime.now(UTC) - row["fetched_at"] < MEDIA_FAILED_RETRY
        ):
            cached[url] = MediaHash(
                url=url,
                status=row["status"],
                phash=row["phash"],
                phash_mirror=row["phash_mirror"],
                error=row["error"],
            )

    async def fetch_one(url: str) -> tuple[MediaHash, int | None]:
        try:
            f = await fetch_url(
                http, _small(url), settings, max_bytes=settings.image_max_bytes, accept="image/*"
            )
            feats = await asyncio.to_thread(image_stage.features, f.body)
            return (
                MediaHash(url=url, status="ok", phash=feats.phash, phash_mirror=feats.phash_mirror),
                feats.dhash,
            )
        except (UnsafeUrl, FetchError) as e:
            return MediaHash(url=url, status="failed", error=str(e)[:200]), None
        except Exception as e:  # noqa: BLE001 - undecodable image etc.
            return MediaHash(url=url, status="failed", error=f"{type(e).__name__}: {e}"[:200]), None

    # the downloads run concurrently: a cold full analysis should not wait 4x for media
    missing = [u for u in urls if u not in cached]
    fetched = await asyncio.gather(*(fetch_one(u) for u in missing))
    for mh, dhash in fetched:
        await conn.execute(
            """insert into x_media (url, status, phash, phash_mirror, dhash, error, fetched_at)
               values ($1, $2, $3, $4, $5, $6, now())
               on conflict (url) do update set status=excluded.status, phash=excluded.phash,
                 phash_mirror=excluded.phash_mirror, dhash=excluded.dhash,
                 error=excluded.error, fetched_at=now()""",
            mh.url,
            mh.status,
            mh.phash,
            mh.phash_mirror,
            dhash,
            mh.error,
        )
        cached[mh.url] = mh
    out = [cached[u] for u in urls]
    return out
