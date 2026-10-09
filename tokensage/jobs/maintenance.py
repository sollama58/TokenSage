"""Cron entrypoint (hourly): requeue expired leases, prune old jobs, stale analyses and
expired lookup_cache rows."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import structlog

from tokensage import queue, recall
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


# lookup_cache rows for one name or phrase (Google News, Bluesky, Wikipedia) are upserted on
# every full read and only checked against their TTL on read, so nothing else removes them.
# The longest TTL is WIKI_TTL (7 days); past LOOKUP_KEEP a row is only a fallback for an
# upstream outage that long. The trend rows (gtrends:seen, xtrends:seen) are single
# long-lived keys and are left alone.
LOOKUP_KEEP = "8 days"
LOOKUP_PREFIXES = ["bsky:%", "gnews:%", "wiki:%"]


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
    ("pruned_usage_rows", _prune_usage, True),
    ("pruned_lookup_cache", _prune_lookup_cache, True),
    ("repaired_logo_hashes", _repair_logo_hashes, False),
    ("recall", _recall, False),
]


async def run_once() -> dict[str, Any]:
    settings = get_settings()
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    stats: dict[str, Any] = {}
    failed: list[str] = []
    try:
        for key, step, required in STEPS:
            try:
                async with pool.acquire() as conn:
                    result = await step(conn)
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
    stats = await run_once()
    log.info("maintenance.done", **stats)
    return 1 if stats["failed_steps"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
