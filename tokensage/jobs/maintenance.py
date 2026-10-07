"""Cron entrypoint (hourly): requeue expired leases, prune old jobs and stale analyses."""

from __future__ import annotations

import asyncio

import structlog

from tokensage import queue, recall
from tokensage.config import get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("jobs.maintenance")


async def run_once() -> dict[str, int]:
    settings = get_settings()
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            requeued = await queue.requeue_expired(conn)
            pruned_jobs = await conn.execute(
                """delete from job where status in ('done','failed')
                   and finished_at < now() - interval '7 days'"""
            )
            # keep the newest 3 versions per (mint, depth); drop older ones past 30 days
            pruned_analyses = await conn.execute(
                """delete from analysis a using (
                     select mint, version,
                            row_number() over (partition by mint, depth order by version desc) rn
                     from analysis) r
                   where a.mint = r.mint and a.version = r.version and r.rn > 3
                     and a.created_at < now() - interval '30 days'"""
            )
            # upstream call counts back a 90-day panel view; the billing cycle needs ~31
            pruned_usage = await conn.execute(
                "delete from upstream_usage where hour < now() - interval '90 days'"
            )
            # the recall number (guide §5.9): how often the last day's analyses resolved a
            # referent, per depth; the series to watch as the gazetteer and rules change
            log.info("recall.daily", **(await recall.summary(conn)))
        return {
            "requeued": requeued,
            "pruned_jobs": int(pruned_jobs.split()[-1]),
            "pruned_analyses": int(pruned_analyses.split()[-1]),
            "pruned_usage_rows": int(pruned_usage.split()[-1]),
        }
    finally:
        await pool.close()


async def main() -> None:
    configure_logging(get_settings().log_level)
    stats = await run_once()
    log.info("maintenance.done", **stats)


if __name__ == "__main__":
    asyncio.run(main())
