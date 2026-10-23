"""Cron entrypoint (hourly): requeue expired leases, prune old jobs and stale analyses,
condense old analyses, prune expired lookup_cache rows and the token family past its
retention (config.py, "retention")."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

import asyncpg
import structlog

from tokensage import envreport, queue, recall
from tokensage.config import get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("jobs.maintenance")

# Statement timeout per step. A cron run has nobody waiting on it, so a prune that is slow
# while the workers are busy gets more room than the pool's 30 s default.
STEP_TIMEOUT_S = 120.0
# The analysis prune works in batches of this many rows and stops starting new ones after
# this long; whatever is left waits for the next run (the cursors keep its place).
PRUNE_BATCH = 5000
PRUNE_BUDGET_S = 60.0
KEEP_VERSIONS = 3


def _count(status: str) -> int:
    return int(status.split()[-1])


async def _requeue(conn: asyncpg.Connection) -> int:
    # a job with no attempts left is failed ("worker died") instead of being re-run for ever
    return await queue.requeue_expired(conn, get_settings().job_max_attempts)


async def _prune_jobs(conn: asyncpg.Connection) -> int:
    return _count(
        await conn.execute(
            """delete from job where status in ('done','failed')
               and finished_at < now() - interval '7 days'""",
            timeout=STEP_TIMEOUT_S,
        )
    )


# A superseded analysis (3 newer versions at its depth) is pruned once it is past 30 days.
# It becomes prunable either when it ages past 30 days (the `analysis_aged` cursor walks rows
# as they cross that line) or when a newer version lands after that (the `analysis_new`
# cursor walks new rows and checks their mint and depth). Each batch commits with its cursor,
# so a slow run loses nothing and the next one carries on: no step ever reads the whole table.
AGED_VICTIMS = """
    select a.mint, a.version from analysis a
    where a.created_at > $1 and a.created_at <= $2
      and (select count(*) from analysis n
           where n.mint = a.mint and n.depth = a.depth and n.version > a.version) >= $3"""
NEW_VICTIMS = """
    select o.mint, o.version
    from (select distinct mint, depth from analysis
          where created_at > $1 and created_at <= $2) k,
         lateral (select mint, version, created_at from analysis
                  where mint = k.mint and depth = k.depth
                  order by version desc offset $3) o
    where o.created_at < now() - interval '30 days'"""


async def _prune_batch(conn: asyncpg.Connection, cursor: str, upto: str, victims_sql: str) -> int:
    """Prune one batch past `cursor` (rows up to `upto`) and move the cursor. Returns the
    rows deleted, or -1 when the cursor has caught up."""
    async with conn.transaction():
        at = await conn.fetchval(
            "select at from maintenance_cursor where name = $1 for update",
            cursor,
            timeout=STEP_TIMEOUT_S,
        )
        # the batch ends at the PRUNE_BATCH-th row's time; rows sharing that time go in too
        end = await conn.fetchval(
            f"""select max(created_at) from (
                  select created_at from analysis
                  where created_at > $1 and created_at <= {upto}
                  order by created_at limit $2) b""",
            at,
            PRUNE_BATCH,
            timeout=STEP_TIMEOUT_S,
        )
        if end is None:
            return -1
        victims = await conn.fetch(victims_sql, at, end, KEEP_VERSIONS, timeout=STEP_TIMEOUT_S)
        deleted = 0
        if victims:
            deleted = _count(
                await conn.execute(
                    """delete from analysis a
                       using unnest($1::text[], $2::int[]) v(mint, version)
                       where a.mint = v.mint and a.version = v.version""",
                    [v["mint"] for v in victims],
                    [v["version"] for v in victims],
                    timeout=STEP_TIMEOUT_S,
                )
            )
        await conn.execute(
            "update maintenance_cursor set at = $2 where name = $1",
            cursor,
            end,
            timeout=STEP_TIMEOUT_S,
        )
        return deleted


async def _prune_analyses(conn: asyncpg.Connection) -> dict[str, Any]:
    # keep the newest 3 versions per (mint, depth); drop older ones past 30 days
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRUNE_BUDGET_S
    passes = [
        # a few minutes behind now(): an insert still in flight carries an earlier created_at
        ("analysis_new", "now() - interval '5 minutes'", NEW_VICTIMS),
        ("analysis_aged", "now() - interval '30 days'", AGED_VICTIMS),
    ]
    deleted, batches = 0, 0
    # one batch per cursor in turn, so a burst of new rows does not starve the backlog
    while passes:
        for p in list(passes):
            n = await _prune_batch(conn, *p)
            if n < 0:
                passes.remove(p)
            else:
                deleted += n
                batches += 1
        if loop.time() >= deadline:
            break
    lag = await conn.fetchrow(
        """select extract(epoch from now() - max(at) filter (where name = 'analysis_new'))
                    as new_s,
                  extract(epoch from now() - interval '30 days'
                          - max(at) filter (where name = 'analysis_aged')) as aged_s
           from maintenance_cursor""",
        timeout=STEP_TIMEOUT_S,
    )
    # how far behind each cursor still is (seconds); the aged one starts at the oldest row
    # and is days behind until the backlog is through
    return {
        "deleted": deleted,
        "batches": batches,
        "new_lag_s": _secs(lag["new_s"]),
        "aged_lag_s": _secs(lag["aged_s"]),
    }


def _secs(v: Any) -> int | None:
    return None if v is None else max(0, int(v))


async def _prune_usage(conn: asyncpg.Connection) -> int:
    # upstream call counts back a 90-day panel view; the billing cycle needs ~31
    return _count(
        await conn.execute(
            "delete from upstream_usage where hour < now() - interval '90 days'",
            timeout=STEP_TIMEOUT_S,
        )
    )


# Condensing (see Settings.analysis_condense_days): every analysis past that age loses the
# parts no later read uses, behind the `analysis_condense` cursor (migration 0023) through
# analysis.created_at, a bounded slice per run like the prunes above. The caveat marks a
# condensed read, so a row is never rewritten twice, and tells a caller who gets one back
# as a stale_analysis why its evidence is empty.
CONDENSE_DROP = ["evidence", "market"]
CONDENSE_DROP_BASIC = ["x", "image", "trend"]
CONDENSED_CAVEAT = "condensed: evidence and market detail left out of this older read"
# Rows rewritten per batch. Each one is a cold, TOASTed document read and written back with
# every index entry: 5,000 at a time (PRUNE_BATCH) ran past STEP_TIMEOUT_S on the production
# database (2026-10-17 17:27 run). A batch that still times out is retried at half the size,
# down to CONDENSE_MIN_BATCH, before the step fails.
CONDENSE_BATCH = 200
CONDENSE_MIN_BATCH = 25
# The condensing backlog (every row past the horizon when it shipped) gets more room than the
# prunes: at the batch size above it still clears it within days.
CONDENSE_BUDGET_S = 120.0


async def _condense_batch(conn: asyncpg.Connection, days: int, size: int) -> int:
    """Condense one batch past the cursor and move it. Returns the rows rewritten, or -1
    when the cursor has caught up with the horizon."""
    async with conn.transaction():
        at = await conn.fetchval(
            "select at from maintenance_cursor where name = 'analysis_condense' for update",
            timeout=STEP_TIMEOUT_S,
        )
        if at is None:  # un-migrated: nothing to walk from
            return -1
        end = await conn.fetchval(
            """select max(created_at) from (
                 select created_at from analysis
                 where created_at > $1 and created_at <= now() - make_interval(days => $2)
                 order by created_at limit $3) b""",
            at,
            days,
            size,
            timeout=STEP_TIMEOUT_S,
        )
        if end is None:
            return -1
        n = _count(
            await conn.execute(
                """update analysis set doc =
                     (doc - $3::text[]
                          - (case when depth = 'basic' then $4::text[] else '{}'::text[] end))
                     || jsonb_build_object('caveats',
                          coalesce(doc->'caveats', '[]'::jsonb) || to_jsonb($5::text))
                   where created_at > $1 and created_at <= $2
                     and not coalesce(doc->'caveats', '[]'::jsonb) ? $5""",
                at,
                end,
                CONDENSE_DROP,
                CONDENSE_DROP_BASIC,
                CONDENSED_CAVEAT,
                timeout=STEP_TIMEOUT_S,
            )
        )
        await conn.execute(
            "update maintenance_cursor set at = $1 where name = 'analysis_condense'",
            end,
            timeout=STEP_TIMEOUT_S,
        )
        return n


async def _condense_analyses(
    conn: asyncpg.Connection, size: int = CONDENSE_BATCH
) -> dict[str, Any] | None:
    days = get_settings().analysis_condense_days
    if days <= 0:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONDENSE_BUDGET_S
    out: dict[str, Any] = {"condensed": 0, "batches": 0}
    while loop.time() < deadline:
        try:
            n = await _condense_batch(conn, days, size)
        except TimeoutError:
            # rolled back with its cursor: the same rows go again, fewer at a time
            if size <= CONDENSE_MIN_BATCH:
                raise
            size = max(CONDENSE_MIN_BATCH, size // 2)
            continue
        if n < 0:
            break
        out["condensed"] += n
        out["batches"] += 1
    lag = await conn.fetchval(
        """select extract(epoch from now() - make_interval(days => $1) - at)
           from maintenance_cursor where name = 'analysis_condense'""",
        days,
        timeout=STEP_TIMEOUT_S,
    )
    out["lag_s"] = _secs(lag)
    out["batch"] = size
    return out


# lookup_cache rows for one name or phrase (Google News, Bluesky, Wikipedia) are upserted on
# every full read and only checked against their TTL on read, so nothing else removes them.
# The longest TTL is WIKI_TTL (7 days); past LOOKUP_KEEP a row is only a fallback for an
# upstream outage that long. The decision-time refreshes (now:, heat:, since rules 0.34.0)
# are minutes-fresh by nature and go with them, as do the full read's trend inputs
# (trendin:, needed only while a consumer still decides on the coin). The trend rows
# (gtrends:seen, xtrends:seen) are single long-lived keys and are left alone.
LOOKUP_KEEP = "8 days"
LOOKUP_PREFIXES = ["bsky:%", "gnews:%", "wiki:%", "now:%", "heat:%", "trendin:%"]


async def _prune_lookup_cache(conn: asyncpg.Connection) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRUNE_BUDGET_S
    deleted = 0
    while True:
        n = _count(
            await conn.execute(
                f"""delete from lookup_cache where ctid in (
                      select ctid from lookup_cache
                      where fetched_at < now() - interval '{LOOKUP_KEEP}'
                        and key like any($1::text[])
                      limit $2)""",
                LOOKUP_PREFIXES,
                PRUNE_BATCH,
                timeout=STEP_TIMEOUT_S,
            )
        )
        deleted += n
        if n < PRUNE_BATCH or loop.time() >= deadline:
            return deleted


# ---------------------------------------------------------------- the token family
# Database audit 2026-10-16 (I1). Two horizons, see Settings.token_detail_retention_days: a
# token's heavy rows go first, the slim token row (which the meta and copycat counts read)
# later. The detail prune walks token.created_at behind the `token_detail` cursor
# (migration 0021), from the oldest token, one bounded slice per run, like the analysis
# prune: no run reads the whole table, and a slow run leaves the rest for the next. A token
# first read long after its launch arrives behind the cursor and keeps its heavy rows until
# the row horizon takes the whole token.
# Each mint keeps its newest analysis at each depth (audit 2026-10-17 M4): pair tokens and
# lineage originals are read back from it (resolve/pair.py, analyzer._prior_reads) whatever
# their age, and /v1/tokens:now needs the full read. And a token read within the detail
# horizon (a new analysis row) is not taken by the row prune, however old its launch.
DETAIL_TABLES = ("token_metadata", "token_read", "token_market")
_NOT_RECENTLY_READ = """not exists (select 1 from analysis a where a.mint = token.mint
                          and a.created_at > now() - make_interval(days => $3))"""


async def _prune_token_detail_batch(conn: asyncpg.Connection, days: int) -> int:
    async with conn.transaction():
        at = await conn.fetchval(
            "select at from maintenance_cursor where name = 'token_detail' for update",
            timeout=STEP_TIMEOUT_S,
        )
        if at is None:  # un-migrated: nothing to walk from
            return -1
        # the batch ends at the PRUNE_BATCH-th token's time; tokens sharing it go in too
        end = await conn.fetchval(
            """select max(created_at) from (
                 select created_at from token
                 where created_at > $1 and created_at <= now() - make_interval(days => $2)
                 order by created_at limit $3) b""",
            at,
            days,
            PRUNE_BATCH,
            timeout=STEP_TIMEOUT_S,
        )
        if end is None:
            return -1
        mints = [
            r["mint"]
            for r in await conn.fetch(
                "select mint from token where created_at > $1 and created_at <= $2",
                at,
                end,
                timeout=STEP_TIMEOUT_S,
            )
        ]
        deleted = _count(
            await conn.execute(
                """delete from analysis a where a.mint = any($1::text[])
                     and exists (select 1 from analysis n where n.mint = a.mint
                                 and n.depth = a.depth and n.version > a.version)""",
                mints,
                timeout=STEP_TIMEOUT_S,
            )
        )
        for table in DETAIL_TABLES:
            deleted += _count(
                await conn.execute(
                    f"delete from {table} where mint = any($1::text[])",
                    mints,
                    timeout=STEP_TIMEOUT_S,
                )
            )
        await conn.execute(
            "update maintenance_cursor set at = $1 where name = 'token_detail'",
            end,
            timeout=STEP_TIMEOUT_S,
        )
        return deleted


async def _prune_tokens(conn: asyncpg.Connection) -> dict[str, Any]:
    settings = get_settings()
    detail_days, row_days = settings.token_detail_retention_days, settings.token_retention_days
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRUNE_BUDGET_S
    out: dict[str, Any] = {"detail_rows": 0, "undated_tokens": 0, "tokens": 0, "batches": 0}
    read_days = detail_days or row_days  # "recently read": an analysis row this young
    if detail_days > 0:
        while loop.time() < deadline:
            n = await _prune_token_detail_batch(conn, detail_days)
            if n < 0:
                break
            out["detail_rows"] += n
            out["batches"] += 1
        # A token whose launch time could never be determined is outside every meta and
        # copycat count (they filter on created_at), so it goes whole on the detail horizon,
        # by when it was first seen (token_undated_idx).
        while loop.time() < deadline:
            n = _count(
                await conn.execute(
                    f"""delete from token where mint in (
                         select mint from token
                         where created_at is null
                           and first_seen_at < now() - make_interval(days => $1)
                           and {_NOT_RECENTLY_READ}
                         limit $2)""",
                    detail_days,
                    PRUNE_BATCH,
                    read_days,
                    timeout=STEP_TIMEOUT_S,
                )
            )
            out["undated_tokens"] += n
            out["batches"] += 1
            if n < PRUNE_BATCH:
                break
    if row_days > 0:
        # The oldest tokens first (token_created_at_idx); ON DELETE CASCADE takes x_ref and
        # whatever heavy rows a late-read token still has. No cursor: a token read long
        # after its launch sits behind any cursor, and this reads only rows it deletes.
        while loop.time() < deadline:
            n = _count(
                await conn.execute(
                    f"""delete from token where mint in (
                         select mint from token
                         where created_at < now() - make_interval(days => $1)
                           and {_NOT_RECENTLY_READ}
                         order by created_at limit $2)""",
                    row_days,
                    PRUNE_BATCH,
                    read_days,
                    timeout=STEP_TIMEOUT_S,
                )
            )
            out["tokens"] += n
            out["batches"] += 1
            if n < PRUNE_BATCH:
                break
    lag = await conn.fetchval(
        """select extract(epoch from now() - make_interval(days => $1) - at)
           from maintenance_cursor where name = 'token_detail'""",
        detail_days,
        timeout=STEP_TIMEOUT_S,
    )
    out["detail_lag_s"] = _secs(lag)
    return out


# The caches keyed by something other than a mint, on the detail horizon from their fetch
# time, once a day: none of them has an index on that time, so each sweep reads its table,
# which an hourly run would do 24 times for the same rows. image rows are shared between
# coins with one logo and go only once no token_metadata points at them any more
# (token_metadata_image_idx); a recurring logo whose row is gone is hashed again.
CACHE_SWEEP_HOUR_UTC = 4
CACHE_SWEEPS: list[tuple[str, str]] = [
    (
        "image",
        """delete from image where ctid in (
             select i.ctid from image i
             where i.analyzed_at < now() - make_interval(days => $1)
               and not exists (select 1 from token_metadata m
                               where m.image_content_key = i.content_key)
             limit $2)""",
    ),
    ("x_tweet", "fetched_at"),
    ("x_profile", "fetched_at"),
    ("x_profile_history", "seen_at"),
    ("x_media", "fetched_at"),
    ("pair_token", "fetched_at"),
    ("fee_recipient", "resolved_at"),
]


async def _sweep_caches(conn: asyncpg.Connection, *, force: bool = False) -> dict[str, int] | None:
    if not force and datetime.now(UTC).hour != CACHE_SWEEP_HOUR_UTC:
        return None
    days = get_settings().token_detail_retention_days
    if days <= 0:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRUNE_BUDGET_S
    out: dict[str, int] = {}
    for table, sql in CACHE_SWEEPS:
        if " " not in sql:  # a column name: the plain time-to-live form
            sql = f"""delete from {table} where ctid in (
                        select ctid from {table}
                        where {sql} < now() - make_interval(days => $1) limit $2)"""
        deleted = 0
        while True:
            n = _count(await conn.execute(sql, days, PRUNE_BATCH, timeout=STEP_TIMEOUT_S))
            deleted += n
            if n < PRUNE_BATCH or loop.time() >= deadline:
                break
        out[table] = deleted
        if loop.time() >= deadline:
            break
    return out


# token.logo_phash is kept by triggers (migration 0017); this re-derives it for the coins
# inside the logo scan window (data/meta.yaml logo_scan_days, 7) in case two concurrent
# writes for one logo still left a copy behind. The window's coins come from
# token_created_at_idx and each one's logo from the token_metadata and image primary keys:
# a join here would read all of token_metadata every hour.
LOGO_REPAIR_WINDOW = "8 days"


async def _repair_logo_hashes(conn: asyncpg.Connection) -> int:
    # `offset 0` keeps the planner on the per-coin primary-key lookups
    rows = await conn.fetch(
        f"""select w.mint, i.phash
              from token w
              join lateral (select tm.image_content_key from token_metadata tm
                             where tm.mint = w.mint offset 0) tm on true
              join image i on i.content_key = tm.image_content_key
             where w.created_at > now() - interval '{LOGO_REPAIR_WINDOW}'
               and w.logo_phash is distinct from i.phash""",
        timeout=STEP_TIMEOUT_S,
    )
    if not rows:
        return 0
    return _count(
        await conn.execute(
            """update token t set logo_phash = u.phash
                 from unnest($1::text[], $2::bigint[]) as u(mint, phash)
                where t.mint = u.mint and t.logo_phash is distinct from u.phash""",
            [r["mint"] for r in rows],
            [r["phash"] for r in rows],
            timeout=STEP_TIMEOUT_S,
        )
    )


async def _recall(conn: asyncpg.Connection) -> None:
    # the recall number (guide §5.9): how often the last day's analyses resolved a
    # referent, per depth; the series to watch as the gazetteer and rules change
    log.info("recall.daily", **(await recall.summary(conn)))


# (stats key, step, required). A required step that fails makes the run exit non-zero so the
# cron shows red; a reporting step only logs. Either way the remaining steps still run.
STEPS: list[tuple[str, Callable[[asyncpg.Connection], Awaitable[Any]], bool]] = [
    ("requeued", _requeue, True),
    ("pruned_jobs", _prune_jobs, True),
    ("pruned_analyses", _prune_analyses, True),
    ("condensed_analyses", _condense_analyses, True),
    ("pruned_usage_rows", _prune_usage, True),
    ("pruned_lookup_cache", _prune_lookup_cache, True),
    ("pruned_tokens", _prune_tokens, True),
    ("swept_caches", _sweep_caches, True),
    ("repaired_logo_hashes", _repair_logo_hashes, False),
    ("recall", _recall, False),
]


# A dropped database shows up as one of these: the step that was running loses its
# connection mid-statement (2026-10-19 02:08 run, in condensed_analyses) and every connect is
# refused until the server is back. A statement timeout (TimeoutError, QueryCanceledError) is
# not one of them: the step itself was too slow.
_LOST = (
    ConnectionError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
)
# How long a run waits for the database to come back after a drop before giving up on it.
RECONNECT_WAIT_S = 180.0
RECONNECT_RETRY_S = 5.0
# The step that lost its connection is retried once when the database is back. These go
# again more gently: if the drop came from the database running short under the step's load,
# the same load would only bring it down a second time.
RETRY_STEPS: dict[str, Callable[[asyncpg.Connection], Awaitable[Any]]] = {
    "condensed_analyses": partial(_condense_analyses, size=CONDENSE_MIN_BATCH),
}


def _lost(e: BaseException) -> bool:
    # asyncpg reports a connection closed under an open transaction as a plain InterfaceError
    return isinstance(e, _LOST) or (
        isinstance(e, asyncpg.InterfaceError) and "connection is closed" in str(e)
    )


async def _wait_for_db(dsn: str) -> bool:
    deadline = time.monotonic() + RECONNECT_WAIT_S
    while True:
        try:
            conn = await asyncpg.connect(dsn, timeout=RECONNECT_RETRY_S)
        except Exception as e:
            if not _lost(e) and not isinstance(e, OSError):
                raise
            if time.monotonic() >= deadline:
                return False
            log.warning("maintenance.db_down", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_RETRY_S)
        else:
            await conn.close()
            return True


async def run_once() -> dict[str, Any]:
    settings = get_settings()
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    stats: dict[str, Any] = {}
    failed: list[str] = []
    db_gone = False  # waited the full RECONNECT_WAIT_S once already: no more waiting

    async def attempt(step: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
        async with pool.acquire() as conn:
            return await step(conn)

    try:
        for key, step, required in STEPS:
            try:
                try:
                    result = await attempt(step)
                except Exception as e:
                    if not _lost(e) or db_gone:
                        raise
                    log.warning(
                        "maintenance.connection_lost", step=key, error=f"{type(e).__name__}: {e}"
                    )
                    if not await _wait_for_db(settings.database_url):
                        db_gone = True
                        raise
                    # the pool's connections died with the server: open fresh ones
                    await pool.expire_connections()
                    log.info("maintenance.reconnected", step=key)
                    result = await attempt(RETRY_STEPS.get(key, step))
            except Exception as e:  # one step must not take down the others
                (log.error if required else log.warning)(
                    "maintenance.step_failed", step=key, error=f"{type(e).__name__}: {e}"
                )
                if required:
                    failed.append(key)
                continue
            if result is not None:
                stats[key] = result
    finally:
        await pool.close()
    stats["failed_steps"] = failed
    return stats


async def main() -> int:
    configure_logging(get_settings().log_level)
    # For TrenchScanner's Admin > Environment, on a connection of its own apart from run_once's.
    await envreport.publish_quietly(get_settings().database_url, "maintenance")
    stats = await run_once()
    log.info("maintenance.done", **stats)
    return 1 if stats["failed_steps"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
