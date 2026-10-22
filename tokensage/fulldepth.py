"""Full-depth helpers for the analyzer: cached X content, cached OCR, the trend index and
news confirmation (guide §4.3, §5.8, §6.3). All database-aware; the engine stays pure."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import asyncpg
import httpx
import structlog

from tokensage import cpu
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
OEMBED_UPGRADE_RETRY = timedelta(minutes=30)  # how often the richer mirrors are asked again
NEWS_TTL = timedelta(hours=1)
NEWS_WINDOW = timedelta(days=2)  # the window Google News is searched over (when:2d)
BLUESKY_TTL = timedelta(hours=1)
WIKI_TTL = timedelta(days=7)  # articles and their descriptions change slowly
WIKI_EMPTY_TTL = timedelta(days=1)  # a name with no article may get one tomorrow
TREND_INDEX_TTL_S = 600
MAX_NEWS_LOOKUPS = 2


# ----------------------------------------------------------------- single flight

# keyed by event loop too: a lock is bound to its loop, and one a closed loop left held must
# not block (or break) the same key on a later loop
_inflight_locks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}
_inflight_refs: dict[tuple[asyncio.AbstractEventLoop, str], int] = {}


@asynccontextmanager
async def _single_flight(key: str) -> AsyncIterator[None]:
    """Serialise the read-fetch-upsert of one cache key inside this process: the 8 worker
    slots analysing a copycat wave all want the same tweet / profile / search at once, and
    only the first should fetch (and pay); the others then find it in the cache."""
    slot = (asyncio.get_running_loop(), key)
    lock = _inflight_locks.setdefault(slot, asyncio.Lock())
    _inflight_refs[slot] = _inflight_refs.get(slot, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _inflight_refs[slot] -= 1
        if _inflight_refs[slot] <= 0:
            _inflight_refs.pop(slot, None)
            _inflight_locks.pop(slot, None)


# ----------------------------------------------------------------- one connection, shared


class _SerialConn:
    """One job's database connection shared by its concurrent full-depth fetches. asyncpg
    runs one query at a time per connection, so each call waits its turn here; the slow
    part (the network) still overlaps. The helpers sharing it use only execute and the
    fetch calls, never a transaction, so interleaving their single statements is safe."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    async def execute(self, *args: Any, **kw: Any) -> Any:
        async with self._lock:
            return await self._conn.execute(*args, **kw)

    async def fetch(self, *args: Any, **kw: Any) -> Any:
        async with self._lock:
            return await self._conn.fetch(*args, **kw)

    async def fetchrow(self, *args: Any, **kw: Any) -> Any:
        async with self._lock:
            return await self._conn.fetchrow(*args, **kw)

    async def fetchval(self, *args: Any, **kw: Any) -> Any:
        async with self._lock:
            return await self._conn.fetchval(*args, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


async def run_all(coros: list[Coroutine[Any, Any, None]]) -> None:
    """Run the coroutines concurrently. The first error cancels the others and is raised
    as it is (not wrapped in a group), as when they ran one after the other."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def serial(conn: asyncpg.Connection) -> asyncpg.Connection:
    """`conn` made safe to share between concurrent tasks of one job (see _SerialConn)."""
    if isinstance(conn, _SerialConn):
        return conn
    return cast(asyncpg.Connection, _SerialConn(conn))


# ----------------------------------------------------------------- X content with caching


async def _paid_x_allowed(conn: asyncpg.Connection, settings: Settings) -> bool:
    """ENABLE_PAID_X, and today's estimated spend still under PAID_X_DAILY_USD_CAP."""
    if not (settings.enable_paid_x and settings.twitterapi_io_key):
        return False
    from tokensage.api import usage

    used = (await usage.today(conn, PAID_X_USAGE_KEY)).requests
    return used * settings.paid_x_usd_per_call < settings.paid_x_daily_usd_cap


async def tweet_cached(
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    settings: Settings,
    tweet_id: str,
    *,
    fresh_engagement: bool = False,
) -> TweetData:
    """The tweet, cached. The first good copy is the record (tweets are immutable apart from
    edits), but it is re-checked every TWEET_RECHECK so a deletion is noticed, and a sparse
    oEmbed copy is upgraded when a richer source answers (asked again at most every
    OEMBED_UPGRADE_RETRY). A deletion confirmed by the paid API is final: deleted tweets do
    not come back, and the free mirrors' stale verdicts are what the paid call corrects.

    Engagement (likes, reposts, replies, quotes, bookmarks, views) is not part of the
    record: it comes from the newest good copy, and `fetched_at` says when that copy was
    fetched (since rules 0.30.0). With fresh_engagement (a young coin) the post is fetched
    again once that copy is X_ENGAGEMENT_REFRESH_S old, on the free mirrors only; a failed
    attempt is retried at most every X_ENGAGEMENT_RETRY_S."""
    async with _single_flight(f"tweet:{tweet_id}"):
        return await _tweet_cached(conn, http, settings, tweet_id, fresh_engagement)


# engagement counts: taken from the newest good copy, never frozen in the record
ENGAGEMENT_FIELDS = ("likes", "replies", "reposts", "views", "quotes", "bookmarks")


def _take_engagement(record: TweetData, src: TweetData) -> None:
    """Counts from a newer copy of the same post (and of its inline quoted / replied-to
    posts). A count the newer source does not report keeps its older value."""
    if src.status != "ok" or src.id != record.id:
        return
    for f in ENGAGEMENT_FIELDS:
        v = getattr(src, f)
        if v is not None:
            setattr(record, f, v)
    if src.fetched_at is not None:
        record.fetched_at = src.fetched_at
    for a in ("quoted", "replied_to"):
        mine, theirs = getattr(record, a), getattr(src, a)
        if mine is not None and theirs is not None:
            _take_engagement(mine, theirs)


def _stamp(t: TweetData, at: datetime) -> None:
    t.fetched_at = at
    for other in (t.quoted, t.replied_to):
        if other is not None and other.status == "ok":
            other.fetched_at = at


def _latest_copy(raw: Any) -> TweetData | None:
    if not isinstance(raw, dict):
        return None
    try:
        return TweetData.from_json(raw)
    except (TypeError, ValueError):
        return None


def engagement_stale(
    as_of: datetime | None, last_try: timedelta | None, settings: Settings, now: datetime
) -> bool:
    """Whether a young coin's post should be fetched again for its counts: the newest copy
    is X_ENGAGEMENT_REFRESH_S old (or its time unknown), and the last attempt, good or not,
    is X_ENGAGEMENT_RETRY_S old."""
    old = as_of is None or (now - as_of).total_seconds() >= settings.x_engagement_refresh_s
    retry = last_try is None or last_try.total_seconds() >= settings.x_engagement_retry_s
    return old and retry


async def _tweet_cached(
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    settings: Settings,
    tweet_id: str,
    fresh_engagement: bool = False,
) -> TweetData:
    row = await conn.fetchrow(
        "select first_snapshot, latest, status, source, fetched_at from x_tweet where tweet_id=$1",
        tweet_id,
    )
    record: TweetData | None = None
    counts_only = False  # the record is good: fetched again only for fresher counts
    now = datetime.now(UTC)
    if row:
        age = now - row["fetched_at"] if row["fetched_at"] else None
        if row["status"] == "ok" and row["first_snapshot"]:
            record = TweetData.from_json(row["first_snapshot"])
            latest = _latest_copy(row["latest"])
            if latest is not None:
                _take_engagement(record, latest)
            # a copy cached before reply support has no reply fields: re-check it now
            # rather than serve it for up to TWEET_RECHECK without them
            current = "replying_to_id" in row["first_snapshot"]
            sparse = row["source"] == "oembed" and age is not None and age >= OEMBED_UPGRADE_RETRY
            if age is not None and age < TWEET_RECHECK and not sparse and current:
                if not (
                    fresh_engagement and engagement_stale(record.fetched_at, age, settings, now)
                ):
                    return record
                counts_only = True
        elif row["status"] == "deleted" and row["source"] == "twitterapi_io":
            return TweetData(id=tweet_id, status="deleted", source=row["source"])
        elif row["status"] in ("deleted", "failed") and age is not None and age < DELETED_RETRY:
            return TweetData(id=tweet_id, status=row["status"], source=row["source"])
    # fresher counts alone are not worth a paid call
    paid_ok = not counts_only and await _paid_x_allowed(conn, settings)
    t = await fetch_tweet(http, tweet_id, paid_key=settings.twitterapi_io_key, allow_paid=paid_ok)
    if t.status == "ok":
        _stamp(t, datetime.now(UTC))
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
            _take_engagement(record, t)
        return record
    return t


# Fields older cache records may lack: filled from a fresh copy without replacing the record.
_BACKFILL = (
    "media_urls",
    "media",
    "quoted_tweet_id",
    "quoted",
    "replying_to_id",
    "replying_to_handle",
)


def _fill_missing(record: TweetData, fresh: TweetData) -> None:
    for f in (*_BACKFILL, "replied_to"):
        if not getattr(record, f) and getattr(fresh, f):
            setattr(record, f, getattr(fresh, f))


async def profile_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, handle: str
) -> ProfileData:
    async with _single_flight(f"profile:{handle.lower()}"):
        return await _profile_cached(conn, http, handle)


async def _profile_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, handle: str
) -> ProfileData:
    row = await conn.fetchrow(
        """select user_id, handle, snapshot, source, fetched_at from x_profile
           where lower(handle) = lower($1) order by fetched_at desc limit 1""",
        handle,
    )
    if row and row["snapshot"] and row["fetched_at"]:
        if datetime.now(UTC) - row["fetched_at"] < PROFILE_TTL:
            return replace(ProfileData.from_json(row["snapshot"]), fetched_at=row["fetched_at"])
    p = await fetch_profile(http, handle)
    if p.status == "failed":
        if row and row["snapshot"]:
            # stale beats nothing; its read time goes out with it (x.author.as_of)
            return replace(ProfileData.from_json(row["snapshot"]), fetched_at=row["fetched_at"])
        return p
    p.fetched_at = datetime.now(UTC)
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
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    settings: Settings,
    x: XInfo | None,
    *,
    fresh_engagement: bool = False,
) -> tuple[TweetData | None, ProfileData | None]:
    """The linked post with its quoted / replied-to posts and its author's profile, or the
    linked profile. The post comes first; the other three then load concurrently (`conn`
    is shared through serial()). fresh_engagement: see tweet_cached."""
    if x is None:
        return None, None
    conn = serial(conn)
    tweet: TweetData | None = None
    profile: ProfileData | None = None
    if x.ref.kind == "tweet" and x.ref.tweet_id:
        tweet = await tweet_cached(
            conn, http, settings, x.ref.tweet_id, fresh_engagement=fresh_engagement
        )
        main = tweet

        async def quoted() -> None:
            # oEmbed and pre-quote cache rows carry only the quoted id: fetch it (cached too)
            assert main.quoted_tweet_id
            main.quoted = await tweet_cached(
                conn, http, settings, main.quoted_tweet_id, fresh_engagement=fresh_engagement
            )

        async def replied() -> None:
            # a reply: the post it answers is usually the context the coin is about
            assert main.replying_to_id
            main.replied_to = await tweet_cached(
                conn, http, settings, main.replying_to_id, fresh_engagement=fresh_engagement
            )

        async def author() -> None:
            nonlocal profile
            assert main.author_handle
            profile = await profile_cached(conn, http, main.author_handle)

        jobs: list[Coroutine[Any, Any, None]] = []
        if (
            main.status == "ok"
            and main.quoted is None
            and main.quoted_tweet_id
            and main.quoted_tweet_id != main.id
        ):
            jobs.append(quoted())
        if (
            main.status == "ok"
            and main.replied_to is None
            and main.replying_to_id
            and main.replying_to_id != main.id
        ):
            jobs.append(replied())
        if main.status == "ok" and main.author_handle:
            jobs.append(author())
        await run_all(jobs)
    elif x.ref.kind == "profile" and x.ref.handle:
        profile = await profile_cached(conn, http, x.ref.handle)
    return tweet, profile


@dataclass
class BasicXLoad:
    """Tweet or profile text for basic-depth interpretation (primary post only)."""

    tweet: TweetData | None = None
    profile: ProfileData | None = None
    caveats: list[str] = field(default_factory=list)


async def _tweet_snapshot_ok(conn: asyncpg.Connection, tweet_id: str) -> TweetData | None:
    """The stored post with its newest counts, as _tweet_cached reads it: the first snapshot
    alone carried the first fetch's counts and as_of into every basic read."""
    row = await conn.fetchrow(
        "select first_snapshot, latest, status from x_tweet where tweet_id=$1",
        tweet_id,
    )
    if row and row["status"] == "ok" and row["first_snapshot"]:
        record = TweetData.from_json(row["first_snapshot"])
        latest = _latest_copy(row["latest"])
        if latest is not None:
            _take_engagement(record, latest)
        return record
    return None


async def _profile_snapshot_ok(conn: asyncpg.Connection, handle: str) -> ProfileData | None:
    row = await conn.fetchrow(
        """select snapshot, status from x_profile
           where lower(handle) = lower($1) order by fetched_at desc limit 1""",
        handle,
    )
    if row and row["status"] == "ok" and row["snapshot"]:
        return ProfileData.from_json(row["snapshot"])
    return None


async def basic_x_load(
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    settings: Settings,
    x: XInfo,
) -> BasicXLoad:
    """Tier A: OK snapshot from x_tweet / x_profile. Tier B: one timed fetch when missing."""
    out = BasicXLoad()
    if not settings.enable_basic_x:
        return out
    sconn = serial(conn)
    if x.ref.kind == "tweet" and x.ref.tweet_id:
        tid = x.ref.tweet_id
        cached = await _tweet_snapshot_ok(sconn, tid)
        if cached is not None:
            out.tweet = cached
            return out
        if not settings.enable_basic_x_fetch:
            return out
        try:
            async with asyncio.timeout(settings.basic_x_timeout_s):
                t = await _tweet_cached(sconn, http, settings, tid, fresh_engagement=False)
        except TimeoutError:
            out.caveats.append(
                f"basic x: tweet fetch timed out after {settings.basic_x_timeout_s:g} s"
            )
            return out
        if t.status == "ok":
            out.tweet = t
        elif t.status == "deleted":
            out.tweet = t
        else:
            out.caveats.append("basic x: linked tweet could not be fetched")
        return out
    if x.ref.kind == "profile" and x.ref.handle:
        handle = x.ref.handle
        prof = await _profile_snapshot_ok(sconn, handle)
        if prof is not None:
            out.profile = prof
            return out
        if not settings.enable_basic_x_fetch:
            return out
        try:
            async with asyncio.timeout(settings.basic_x_timeout_s):
                p = await _profile_cached(sconn, http, handle)
        except TimeoutError:
            out.caveats.append(
                f"basic x: profile fetch timed out after {settings.basic_x_timeout_s:g} s"
            )
            return out
        if p.status == "ok":
            out.profile = p
        else:
            out.caveats.append("basic x: linked profile could not be fetched")
    return out


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
_trend_lock_slot: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _trend_lock() -> asyncio.Lock:
    """The trend-index lock, one per event loop: a lock is bound to its loop, and one left held
    by a loop that closed mid-rebuild (a test's, or a restarted worker's) would otherwise make
    every later loop's acquire fail."""
    global _trend_lock_slot
    loop = asyncio.get_running_loop()
    if _trend_lock_slot is None or _trend_lock_slot[0] is not loop:
        _trend_lock_slot = (loop, asyncio.Lock())
    return _trend_lock_slot[1]


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
    TREND_INDEX_TTL_S; concurrent jobs reuse the old index while one of them rebuilds it.

    In the analyzer worker a background task (refresh_trend_index) keeps the index fresh, so a
    job only builds it when there is none yet (just after a start). A rebuild inside a job used
    to run within that job's deadline, cancelled with it and so retried by the next job."""
    global _trend_cache
    now = time.monotonic()
    if _trend_cache and (_refresher_running or now - _trend_cache[0] < TREND_INDEX_TTL_S):
        return _aged(_trend_cache[1], now - _trend_cache[0])
    if _trend_cache:
        # expired: serve the old index unless nobody is rebuilding it yet
        if _trend_lock().locked():
            return _aged(_trend_cache[1], now - _trend_cache[0])
    async with _trend_lock():
        # the first jobs after a worker start all arrive here with no index: one builds it
        if _trend_cache and time.monotonic() - _trend_cache[0] < TREND_INDEX_TTL_S:
            return _trend_cache[1]
        return await _build_trend_index(conn, http)


# An index this old is served with its sources marked stale: rebuilds have been failing, and
# a source's status from build time ("ok") would otherwise vouch for lists hours old (audit
# 2026-10-19).
TREND_INDEX_STALE_S = 3 * TREND_INDEX_TTL_S


def _aged(idx: trends.TrendIndex, age_s: float) -> trends.TrendIndex:
    if age_s < TREND_INDEX_STALE_S or not any(s.status == "ok" for s in idx.sources):
        return idx
    old = copy.copy(idx)
    detail = f"trend index not rebuilt for {int(age_s // 60)} min"
    old.sources = [
        replace(s, status="stale", detail=detail) if s.status == "ok" else s for s in idx.sources
    ]
    return old


# Set while a worker's refresher keeps the index fresh (jobs then never rebuild an old one).
_refresher_running = False


async def refresh_trend_index(pool: asyncpg.Pool, http: httpx.AsyncClient) -> None:
    """Rebuild the trend index every half TREND_INDEX_TTL_S, off every job's path. Runs until
    cancelled; a failed rebuild keeps the old index and tries again next time."""
    global _refresher_running
    _refresher_running = True
    try:
        while True:
            age = None if _trend_cache is None else time.monotonic() - _trend_cache[0]
            if age is None or age >= TREND_INDEX_TTL_S / 2:
                try:
                    # the connection first: jobs waiting on the lock at a start hold theirs
                    async with pool.acquire() as conn, _trend_lock():
                        await _build_trend_index(conn, http)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - the old index serves until the next try
                    log.warning("trends.refresh_failed", error=f"{type(e).__name__}: {e}"[:300])
            await asyncio.sleep(30)
    finally:
        _refresher_running = False


async def _build_trend_index(
    conn: asyncpg.Connection, http: httpx.AsyncClient | None
) -> trends.TrendIndex:
    """Fetch the sources and build the index (call with _trend_lock held)."""
    global _trend_cache
    wiki_terms, wiki_status = await _wiki_trends(conn)
    g_terms, g_status = await _google_trends(conn, http)
    x_terms, x_status = await _x_trends(conn, http)
    all_terms = wiki_terms + g_terms + x_terms
    statuses = [wiki_status, g_status, x_status]
    try:
        all_terms = await _with_history(conn, all_terms, statuses)
    except (asyncpg.PostgresError, OSError) as e:
        log.info("trends.history_failed", error=type(e).__name__)
    # the matcher build is CPU work: off the event loop, so the other jobs keep moving
    idx = await cpu.run(trends.TrendIndex, all_terms, load_knowledge(), statuses)
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


HISTORY_DAYS = 30  # trend.terms[].days_listed: the days before yesterday it looks back
HISTORY_SOURCES = ("wikipedia", "google_trends", "x_trends")


async def _with_history(
    conn: asyncpg.Connection,
    terms: list[trends.TrendTerm],
    statuses: list[trends.SourceStatus],
) -> list[trends.TrendTerm]:
    """Each term with days_listed: the distinct UTC days in the HISTORY_DAYS before yesterday
    on which any list source had it (trend_term). Today and yesterday are left out: they are
    the run the term is on now. Each source status gets history_days, the days of its own
    list kept in that window (Google Trends and X are recorded from rules 0.33.0 on, so a
    term's days_listed undercounts until they have HISTORY_DAYS)."""
    keys = sorted({t.term.lower() for t in terms})
    rows = await conn.fetch(
        """select lower(term) as t, count(distinct day) as n from trend_term
           where source = any($2::text[]) and day >= (now() at time zone 'utc')::date - $3::int - 1
             and day < (now() at time zone 'utc')::date - 1 and lower(term) = any($1::text[])
           group by 1""",
        keys,
        list(HISTORY_SOURCES),
        HISTORY_DAYS,
    )
    listed = {r["t"]: int(r["n"]) for r in rows}
    cover = {
        r["source"]: int(r["n"])
        for r in await conn.fetch(
            """select source, count(distinct day) as n from trend_term
               where day >= (now() at time zone 'utc')::date - $1::int - 1
                 and day < (now() at time zone 'utc')::date - 1
               group by source""",
            HISTORY_DAYS,
        )
    }
    for st in statuses:
        st.history_days = cover.get(st.source, 0)
    return [replace(t, days_listed=listed.get(t.term.lower(), 0)) for t in terms]


async def _record_history(
    conn: asyncpg.Connection, source: str, rows: list[tuple[str, float, date]]
) -> None:
    """One trend_term row per (term, day) a list source had it: the history days_listed
    counts. score is the strongest reading of the day (Google Trends: searches; X: best
    rank, lower is better, so stored negated)."""
    if not rows:
        return
    await conn.executemany(
        """insert into trend_term (term, source, score, first_seen, day, views)
           values ($1, $2, $3, $4, $4, $5)
           on conflict (term, source, day) do update
             set score = greatest(trend_term.score, excluded.score)""",
        [(term.lower(), source, score, day, int(abs(score))) for term, score, day in rows],
    )


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
            direction="rising" if r["day"] == latest else "falling",
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
            await _record_history(
                conn,
                "google_trends",
                [(it.term, float(it.traffic), now.date()) for items in got for it in items],
            )
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
            direction=_search_direction(v, polled_at),
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
            await _record_history(
                conn,
                "x_trends",
                [
                    (str(v["term"]), -float(v["rank"]), _parse_iso(v.get(at), now).date())
                    for v in seen
                    for at in ("seen_at", "last_at")
                ],
            )
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
            direction=v.get("direction"),
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
    listed in (any region), when it was first and last listed, and its direction (see
    _x_direction)."""
    by: dict[str, dict[str, Any]] = {}
    newest: datetime | None = None
    for lists in pages:
        for tl in lists:
            if now - tl.at >= XTRENDS_KEEP:
                continue
            newest = tl.at if newest is None else max(newest, tl.at)
            hour = tl.at.strftime("%Y-%m-%dT%H")
            for i, raw in enumerate(tl.terms, 1):
                term = xtrends.readable(raw)
                if term is None or gnews._CRYPTO.search(term):
                    continue
                d = by.get(term.lower())
                if d is None:
                    by[term.lower()] = {
                        "term": term,
                        "label": raw,
                        "rank": i,
                        "hours": {hour},
                        "first": tl.at,
                        "first_rank": i,
                        "last": tl.at,
                        "last_rank": i,
                    }
                    continue
                d["rank"] = min(d["rank"], i)
                d["hours"].add(hour)
                if tl.at < d["first"]:
                    d["first"], d["first_rank"] = tl.at, i
                elif tl.at == d["first"]:
                    d["first_rank"] = min(d["first_rank"], i)
                if tl.at > d["last"]:
                    d["last"], d["last_rank"] = tl.at, i
                elif tl.at == d["last"]:
                    d["last_rank"] = min(d["last_rank"], i)
    return [
        {
            "term": d["term"],
            "label": d["label"],
            "rank": d["rank"],
            "hours": len(d["hours"]),
            "seen_at": d["first"].isoformat(),
            "last_at": d["last"].isoformat(),
            "direction": _x_direction(d, newest or now),
        }
        for d in by.values()
    ]


XTRENDS_DROPPED = timedelta(minutes=90)  # not on the newest hourly list (with slack)
XTRENDS_SLIPPED = 10  # places below its best rank on its latest list


def _x_direction(d: dict[str, Any], newest: datetime) -> str:
    """falling: off the newest hourly list, or 10+ places below its best rank on its latest
    list; rising: on the newest list higher than where it first appeared; else steady."""
    if newest - d["last"] > XTRENDS_DROPPED or d["last_rank"] >= d["rank"] + XTRENDS_SLIPPED:
        return "falling"
    if d["last_rank"] < d["first_rank"]:
        return "rising"
    return "steady"


GTRENDS_DROPPED = timedelta(minutes=5)  # absent from the latest poll (with clock slack)


def _search_direction(v: dict[str, Any], polled_at: datetime | None) -> str | None:
    """falling: not on the feed at the latest poll; rising: more searches than when first
    seen; else steady. None for searches stored before rules 0.33.0 kept last_at."""
    last = v.get("last_at")
    if last is None or polled_at is None:
        return None
    if polled_at - _parse_iso(last, polled_at) > GTRENDS_DROPPED:
        return "falling"
    first = v.get("first_traffic")
    if first is not None and int(v.get("traffic") or 0) > int(first):
        return "rising"
    return "steady"


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
            "first_traffic": it.traffic,
            "seen_at": started.isoformat(),
            "last_at": now.isoformat(),
            "headline": it.headline,
            "geo": it.geo,
        }
        return
    have.setdefault("first_traffic", int(have.get("traffic") or 0))
    have["last_at"] = now.isoformat()
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
    async with _single_flight(key):
        return await _news_lookup(conn, http, key, term, exact)


async def _news_lookup(
    conn: asyncpg.Connection, http: httpx.AsyncClient, key: str, term: str, exact: bool
) -> NewsLookup | None:
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
    async with _single_flight(key):
        return await _bsky_for(conn, http, key, phrase)


async def _bsky_for(
    conn: asyncpg.Connection, http: httpx.AsyncClient, key: str, phrase: str
) -> BlueskyLookup | None:
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
    async with _single_flight(key):
        return await _wiki_search(conn, http, key, query)


async def _wiki_search(
    conn: asyncpg.Connection, http: httpx.AsyncClient, key: str, query: str
) -> list[wikipedia.WikiPage] | None:
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
            feats = await cpu.run(image_stage.features, f.body)
            return (
                MediaHash(url=url, status="ok", phash=feats.phash, phash_mirror=feats.phash_mirror),
                feats.dhash,
            )
        except UnsafeUrl as e:
            from tokensage.net.safe_fetch import public_unsafe_detail

            return MediaHash(url=url, status="failed", error=public_unsafe_detail(e)), None
        except FetchError as e:
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
