"""Cron entrypoint: refresh knowledge tables (trends daily, known coins weekly).

Phase 1: a no-op that proves the cron wiring. Phase 4 fills it in (guide §4.4, §5.8).
"""

from __future__ import annotations

import asyncio

import structlog

from tokensage.config import get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("jobs.knowledge")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            n_trend = await conn.fetchval("select count(*) from trend_term")
            n_known = await conn.fetchval("select count(*) from known_coin")
        log.info("knowledge.noop", trend_terms=n_trend, known_coins=n_known)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
