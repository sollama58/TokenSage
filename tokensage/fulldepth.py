"""Full-depth helpers for the analyzer: cached X content, cached OCR, the trend index and
news confirmation (guide §4.3, §5.8, §6.3). All database-aware; the engine stays pure."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage.api.schemas import XInfo
from tokensage.config import Settings
from tokensage.engine import ocr, trends, vision
from tokensage.engine.knowledge import load_knowledge
from tokensage.sources import bluesky, gnews, gtrends, wikipedia, xtrends
from tokensage.sources.x import ProfileData, TweetData, fetch_profile, fetch_tweet
from tokensage.versions import PAID_X_USAGE_KEY as PAID_X_USAGE_KEY

log = structlog.get_logger("fulldepth")

PROFILE_TTL = timedelta(hours=12)
TWEET_RECHECK = timedelta(hours=6)
DELETED_RETRY = timedelta(hours=1)
NEWS_TTL = timedelta(hours=1)
NEWS_WINDOW = timedelta(days=2)  # the window Google News is searched over (when:2d)
BLUESKY_TTL = timedelta(hours=1)
WIKI_TTL = timedelta(days=7)  # articles and their descriptions change slowly
WIKI_EMPTY_TTL = timedelta(days=1)  # a name with no article may get one tomorrow
TREND_INDEX_TTL_S = 600
MAX_NEWS_LOOKUPS = 2


# ----------------------------------------------------------------- X content with caching


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
            # a copy cached before reply support has no reply fields: re-check it now
            # rather than serve it for up to TWEET_RECHECK without them
            current = "replying_to_id" in row["first_snapshot"]
            if age is not None and age < TWEET_RECHECK and row["source"] != "oembed" and current:
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


async def vision_cached(
    conn: asyncpg.Connection, content_key: str | None, model: str
) -> vision.VisionResult | None:
    """The logo's labels from an earlier analysis by this same model and head, if any."""
    if not content_key:
        return None
    raw = await conn.fetchval("select labels from image where content_key=$1", content_key)
    res = vision.from_json(raw)
    return res if res is not None and res.model == model else None


async def persist_vision(
    conn: asyncpg.Connection, content_key: str, res: vision.VisionResult
) -> None:
    await conn.execute(
        "update image set labels=$2 where content_key=$1", content_key, vision.to_json(res)
    )


# ----------------------------------------------------------------- trend index


_trend_cache: tuple[float, trends.TrendIndex] | None = None
_trend_lock = asyncio.Lock()
GTRENDS_KEY = "gtrends:seen"  # lookup_cache row: the Google Trends searches seen lately
GTRENDS_KEEP = timedelta(hours=48)
GTRENDS_POLL = timedelta(minutes=9)  # just under the index TTL: one poll per rebuild
# the knowledge cron loads yesterday's Wikipedia top-1000 at 03:17 UTC, so the newest day is
# normally 1-2 days old; older than this and the cron is not running (or Wikipedia is down)
WIKI_STALE_DAYS = 3


async def trend_index(
    conn: asyncpg.Connection, http: httpx.AsyncClient | None = None
) -> trends.TrendIndex:
    """Wikipedia's spiking articles (daily, from the knowledge cron) plus the Google Trends
    searches of the last two days and X's trending topics of the last day (both polled here,
    when an http client is given), with each source's status. Rebuilt every
    TREND_INDEX_TTL_S; concurrent jobs reuse the old index while one of them rebuilds it."""
    global _trend_cache
    now = time.monotonic()
    if _trend_cache and now - _trend_cache[0] < TREND_INDEX_TTL_S:
        return _trend_cache[1]
    if _trend_cache:
        # expired: serve the old index unless nobody is rebuilding it yet
        if _trend_lock.locked():
            return _trend_cache[1]
    async with _trend_lock:
        # the first jobs after a worker start all arrive here with no index: one builds it
        if _trend_cache and time.monotonic() - _trend_cache[0] < TREND_INDEX_TTL_S:
            return _trend_cache[1]
        wiki_terms, wiki_status = await _wiki_trends(conn)
        g_terms, g_status = await _google_trends(conn, http)
        x_terms, x_status = await _x_trends(conn, http)
        idx = trends.TrendIndex(
            wiki_terms + g_terms + x_terms, load_knowledge(), [wiki_status, g_status, x_status]
        )
        for st in idx.sources:
            log.info(
                "trends.source",
                source=st.source,
                status=st.status,
                terms=st.terms,
                as_of=st.as_of.isoformat() if st.as_of else None,
                detail=st.detail,
            )
        _trend_cache = (time.monotonic(), idx)
        return idx


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


async def _wiki_trends(
    conn: asyncpg.Connection,
) -> tuple[list[trends.TrendTerm], trends.SourceStatus]:
    latest = await conn.fetchval("select max(day) from trend_term where source='wikipedia'")
    if latest is None:
        return [], trends.SourceStatus(
            "wikipedia", "unavailable", detail="no pageview data loaded (knowledge cron)"
        )
    rows = await conn.fetch(
        """with best as (
             -- one row per term (its strongest day): duplicates across days would otherwise
             -- overwrite each other in the index, last (weakest) wins
             select distinct on (term) term, spike, views, day from trend_term
             where source='wikipedia' and day >= $1::date - 2
               and (spike >= 2 or views >= 150000)
             order by term, spike desc nulls last)
           select term, spike, views, day from best
           order by spike desc nulls last limit 1500""",
        latest,
    )
    terms = [
        trends.TrendTerm(
            r["term"],
            float(r["spike"] or 1.0),
            int(r["views"] or 0),
            seen_at=_day_start(r["day"]),
        )
        for r in rows
    ]
    age = (datetime.now(UTC).date() - latest).days
    stale = age > WIKI_STALE_DAYS
    return terms, trends.SourceStatus(
        "wikipedia",
        "stale" if stale else "ok",
        as_of=_day_start(latest),
        terms=len(terms),
        detail=f"newest daily top-1000 is {age} days old" if stale else None,
    )


async def _google_trends(
    conn: asyncpg.Connection, http: httpx.AsyncClient | None
) -> tuple[list[trends.TrendTerm], trends.SourceStatus]:
    """Poll the Google Trends feeds (when the last poll is older than GTRENDS_POLL) and merge
    them into what was seen in the last GTRENDS_KEEP. A search keeps the time it was first
    listed (its seen_at) and the highest search count seen since."""
    now = datetime.now(UTC)
    row = await conn.fetchrow(
        "select value, fetched_at from lookup_cache where key=$1", GTRENDS_KEY
    )
    seen: dict[str, dict[str, Any]] = {
        str(d["term"]).lower(): dict(d) for d in (row["value"] if row else []) or []
    }
    polled_at: datetime | None = row["fetched_at"] if row else None
    status: trends.SourceState = "ok"
    detail: str | None = None
    if http is None:
        status, detail = "skipped", "not polled by this process"
    elif polled_at is None or now - polled_at >= GTRENDS_POLL:
        results = await asyncio.gather(
            *(gtrends.trending(http, g) for g in gtrends.GEOS), return_exceptions=True
        )
        got = [r for r in results if isinstance(r, list)]
        if got:
            for items in got:
                for it in items:
                    _merge_search(seen, it, now)
            seen = {
                k: v
                for k, v in seen.items()
                if now - _parse_iso(v.get("seen_at"), now) < GTRENDS_KEEP
            }
            await conn.execute(
                """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
                   on conflict (key) do update set value=excluded.value, fetched_at=now()""",
                GTRENDS_KEY,
                list(seen.values()),
            )
            polled_at = now
            if len(got) < len(results):
                detail = f"{len(results) - len(got)} of {len(results)} country feeds failed"
        else:
            errs = [type(r).__name__ for r in results if isinstance(r, BaseException)]
            log.info("gtrends.poll_failed", errors=errs[:4])
            status = "failed" if not seen else "stale"
            detail = "feed unavailable" + (
                f"; using the searches seen until {polled_at:%Y-%m-%d %H:%M} UTC"
                if seen and polled_at
                else ""
            )
    terms = [
        trends.TrendTerm(
            str(v["term"]),
            0.0,
            int(v.get("traffic") or 0),
            source="google_trends",
            seen_at=_parse_iso(v.get("seen_at"), now),
            headline=v.get("headline"),
        )
        for v in seen.values()
        if now - _parse_iso(v.get("seen_at"), now) < GTRENDS_KEEP
    ]
    if status == "ok" and polled_at is not None and now - polled_at > 6 * GTRENDS_POLL:
        status, detail = "stale", f"last polled {polled_at:%Y-%m-%d %H:%M} UTC"
    return terms, trends.SourceStatus(
        "google_trends", status, as_of=polled_at, terms=len(terms), detail=detail
    )


XTRENDS_KEY = "xtrends:seen"  # lookup_cache row: X's trending topics of the last day
XTRENDS_KEEP = timedelta(hours=24)
XTRENDS_POLL = timedelta(minutes=20)  # trends24 adds one list an hour


async def _x_trends(
    conn: asyncpg.Connection, http: httpx.AsyncClient | None
) -> tuple[list[trends.TrendTerm], trends.SourceStatus]:
    """X's trending topics (trends24, every XTRENDS_POLL). Each page holds the whole day's
    hourly lists, so a poll rebuilds the set: per topic its best rank, the hourly lists it was
    on, and when it first appeared. The stored set is used while the pages are down."""
    now = datetime.now(UTC)
    row = await conn.fetchrow(
        "select value, fetched_at from lookup_cache where key=$1", XTRENDS_KEY
    )
    seen: list[dict[str, Any]] = list((row["value"] if row else None) or [])
    polled_at: datetime | None = row["fetched_at"] if row else None
    status: trends.SourceState = "ok"
    detail: str | None = None
    if http is None:
        status, detail = "skipped", "not polled by this process"
    elif polled_at is None or now - polled_at >= XTRENDS_POLL:
        results = await asyncio.gather(
            *(xtrends.trending(http, r) for r in xtrends.REGIONS), return_exceptions=True
        )
        got = [r for r in results if isinstance(r, list)]
        if got:
            seen = _merge_x_lists(got, now)
            await conn.execute(
                """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
                   on conflict (key) do update set value=excluded.value, fetched_at=now()""",
                XTRENDS_KEY,
                seen,
            )
            polled_at = now
            if len(got) < len(results):
                detail = f"{len(results) - len(got)} of {len(results)} region pages failed"
        else:
            errs = [type(r).__name__ for r in results if isinstance(r, BaseException)]
            log.info("xtrends.poll_failed", errors=errs[:4])
            status = "failed" if not seen else "stale"
            detail = "trend pages unavailable" + (
                f"; using the topics seen until {polled_at:%Y-%m-%d %H:%M} UTC"
                if seen and polled_at
                else ""
            )
    terms = [
        trends.TrendTerm(
            str(v["term"]),
            0.0,
            int(v.get("hours") or 1),
            source="x_trends",
            seen_at=_parse_iso(v.get("seen_at"), now),
            rank=int(v.get("rank") or 50),
        )
        for v in seen
        if now - _parse_iso(v.get("last_at"), now) < XTRENDS_KEEP
    ]
    if status == "ok" and polled_at is not None and now - polled_at > 6 * XTRENDS_POLL:
        status, detail = "stale", f"last polled {polled_at:%Y-%m-%d %H:%M} UTC"
    return terms, trends.SourceStatus(
        "x_trends", status, as_of=polled_at, terms=len(terms), detail=detail
    )


def _merge_x_lists(pages: list[list[xtrends.TrendList]], now: datetime) -> list[dict[str, Any]]:
    """One record per topic across regions and hours: best rank, the distinct hours it was
    listed in (any region), when it was first and last listed."""
    by: dict[str, dict[str, Any]] = {}
    for lists in pages:
        for tl in lists:
            if now - tl.at >= XTRENDS_KEEP:
                continue
            hour = tl.at.strftime("%Y-%m-%dT%H")
            for i, raw in enumerate(tl.terms, 1):
                term = xtrends.readable(raw)
                if term is None or gnews._CRYPTO.search(term):
                    continue
                d = by.setdefault(
                    term.lower(),
                    {
                        "term": term,
                        "label": raw,
                        "rank": i,
                        "hours": set(),
                        "first": tl.at,
                        "last": tl.at,
                    },
                )
                d["rank"] = min(d["rank"], i)
                d["hours"].add(hour)
                d["first"] = min(d["first"], tl.at)
                d["last"] = max(d["last"], tl.at)
    return [
        {
            "term": d["term"],
            "label": d["label"],
            "rank": d["rank"],
            "hours": len(d["hours"]),
            "seen_at": d["first"].isoformat(),
            "last_at": d["last"].isoformat(),
        }
        for d in by.values()
    ]


def _merge_search(
    seen: dict[str, dict[str, Any]], it: gtrends.TrendingSearch, now: datetime
) -> None:
    key = it.term.lower()
    started = it.started_at or now
    have = seen.get(key)
    if have is None:
        seen[key] = {
            "term": it.term,
            "traffic": it.traffic,
            "seen_at": started.isoformat(),
            "headline": it.headline,
            "geo": it.geo,
        }
        return
    have["traffic"] = max(int(have.get("traffic") or 0), it.traffic)
    if started < _parse_iso(have.get("seen_at"), now):
        have["seen_at"] = started.isoformat()
    have["headline"] = have.get("headline") or it.headline


def _parse_iso(raw: Any, default: datetime) -> datetime:
    try:
        d = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return default
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ----------------------------------------------------------------- news confirmation


@dataclass
class NewsLookup:
    headlines: list[dict[str, Any]]
    as_of: datetime  # when Google News was last asked
    stale: bool  # Google News failed just now; these are older cached headlines


async def news_lookup(
    conn: asyncpg.Connection, http: httpx.AsyncClient, term: str, exact: bool = False
) -> NewsLookup | None:
    """Recent Google News headlines for a term (cached an hour), with when they were fetched.
    exact: search the quoted phrase, so "le chonk" does not return stories about "le" and
    "chonk" separately. None when Google News failed and nothing is cached."""
    key = f"gnews:{'q:' if exact else ''}{term.lower()}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and datetime.now(UTC) - row["fetched_at"] < NEWS_TTL:
        return NewsLookup(list(row["value"]), row["fetched_at"], stale=False)
    heads = await gnews.search(http, f'"{term}"' if exact else term)
    if heads is None:
        if not row:
            return None
        # an older cached list stands in for an outage, but only its headlines that are still
        # inside the search window: a month-old story is not "in the news"
        cutoff = datetime.now(UTC) - NEWS_WINDOW
        recent = [
            h
            for h in row["value"]
            if (trends._published(h) or row["fetched_at"]) >= cutoff  # undated: as of the fetch
        ]
        return NewsLookup(recent, row["fetched_at"], stale=True)
    value = [{"title": h.title, "source": h.source, "published": h.published} for h in heads]
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        value,
    )
    return NewsLookup(value, datetime.now(UTC), stale=False)


async def news_for(
    conn: asyncpg.Connection, http: httpx.AsyncClient, term: str, exact: bool = False
) -> list[dict[str, Any]] | None:
    """news_lookup's headlines alone (an older cached list when Google News is down)."""
    found = await news_lookup(conn, http, term, exact)
    return found.headlines if found is not None else None


@dataclass
class BlueskyLookup:
    posts: list[dict[str, Any]]
    as_of: datetime  # when Bluesky was last asked
    stale: bool  # Bluesky failed just now; these are older cached posts


async def bsky_for(
    conn: asyncpg.Connection, http: httpx.AsyncClient, phrase: str
) -> BlueskyLookup | None:
    """The newest Bluesky posts with the quoted phrase (cached an hour), with when they were
    fetched. A stale copy beats nothing when Bluesky is down; None when nothing is cached."""
    key = f"bsky:q:{phrase.lower()}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and datetime.now(UTC) - row["fetched_at"] < BLUESKY_TTL:
        return BlueskyLookup(list(row["value"]), row["fetched_at"], stale=False)
    posts = await bluesky.search(http, phrase)
    if posts is None:
        return BlueskyLookup(list(row["value"]), row["fetched_at"], stale=True) if row else None
    value = [
        {
            "text": p.text,
            "created_at": p.created_at,
            "likes": p.likes,
            "reposts": p.reposts,
            "author": p.author,
            "uri": p.uri,
        }
        for p in posts
    ]
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        value,
    )
    return BlueskyLookup(value, datetime.now(UTC), stale=False)


# ----------------------------------------------------------------- Wikipedia lookups


async def wiki_search(
    conn: asyncpg.Connection, http: httpx.AsyncClient, query: str
) -> list[wikipedia.WikiPage] | None:
    """Wikipedia's top articles for a name, cached in lookup_cache (a week; a day when
    nothing was found). A stale copy beats nothing when Wikipedia is down."""
    key = f"wiki:{query.lower()}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row is not None:
        cached = [wikipedia.WikiPage.from_json(d) for d in row["value"] or []]
        ttl = WIKI_TTL if cached else WIKI_EMPTY_TTL
        if datetime.now(UTC) - row["fetched_at"] < ttl:
            return cached
    pages = await wikipedia.search(http, query)
    if pages is None:
        return [wikipedia.WikiPage.from_json(d) for d in row["value"] or []] if row else None
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        [p.to_json() for p in pages],
    )
    return pages


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
