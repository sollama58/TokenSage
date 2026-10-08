"""Cron entrypoint (hourly): requeue expired leases, prune old jobs and stale analyses."""

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
# A superseded analysis becomes prunable when it crosses 30 days or when a newer version
# lands. The prune looks only at rows that changed state within this window, not the whole
# table; an outage longer than this leaves a few superseded rows behind, which costs disk only.
PRUNE_WINDOW = "3 days"


def _count(status: str) -> int:
    return int(status.split()[-1])


async def _requeue(conn: asyncpg.Connection) -> int:
    return await queue.requeue_expired(conn)


async def _prune_jobs(conn: asyncpg.Connection) -> int:
    return _count(
        await conn.execute(
            """delete from job where status in ('done','failed')
               and finished_at < now() - interval '7 days'""",
            timeout=STEP_TIMEOUT_S,
        )
    )


async def _prune_analyses(conn: asyncpg.Connection) -> int:
    # keep the newest 3 versions per (mint, depth); drop older ones past 30 days. The
    # candidates are rows that just crossed 30 days and the old rows of mints analysed again
    # recently: a window over the whole table sorts every analysis ever written, every hour.
    # `offset 0` keeps the old rows of recent mints a per-mint key lookup, and the victims are
    # deleted by key, so neither half turns into a scan of the whole table.
    victims = await conn.fetch(
        f"""with cand as (
              select mint, version, depth from analysis
              where created_at < now() - interval '30 days'
                and created_at >= now() - interval '30 days' - interval '{PRUNE_WINDOW}'
              union
              select r.mint, o.version, o.depth
              from (select distinct mint from analysis
                    where created_at >= now() - interval '{PRUNE_WINDOW}') r,
                   lateral (select version, depth, created_at from analysis
                            where mint = r.mint offset 0) o
              where o.created_at < now() - interval '30 days')
            select mint, version from cand c
            where (select count(*) from analysis n
                   where n.mint = c.mint and n.depth = c.depth and n.version > c.version) >= 3""",
        timeout=STEP_TIMEOUT_S,
    )
    if not victims:
        return 0
    return _count(
        await conn.execute(
            """delete from analysis a using unnest($1::text[], $2::int[]) v(mint, version)
               where a.mint = v.mint and a.version = v.version""",
            [v["mint"] for v in victims],
            [v["version"] for v in victims],
            timeout=STEP_TIMEOUT_S,
        )
    )


async def _prune_usage(conn: asyncpg.Connection) -> int:
    # upstream call counts back a 90-day panel view; the billing cycle needs ~31
    return _count(
        await conn.execute(
            "delete from upstream_usage where hour < now() - interval '90 days'",
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
