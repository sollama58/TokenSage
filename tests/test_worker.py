"""Worker loop: processes jobs, survives a bad job, stops cleanly and releases work on stop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.conftest import needs_db
from tokensage import queue
from tokensage.config import Settings
from tokensage.db import create_pool
from tokensage.worker import Worker

pytestmark = needs_db
MINT = "PuuQ746gUr4mUkMzZhaYkgQomxQNpVgQfdEmZUVpump"


@pytest.fixture
async def pool(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_db, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


def _settings(db: str) -> Settings:
    return Settings(
        database_url=db,
        worker_poll_interval_s=0.1,
        job_max_attempts=2,
        _env_file=None,  # type: ignore[call-arg]
    )


async def test_worker_processes_jobs_and_handles_unknown_kind(
    pool: asyncpg.Pool, migrated_db: str
) -> None:
    w = Worker(pool, _settings(migrated_db))
    task = asyncio.create_task(w.run())
    try:
        async with pool.acquire() as c:
            good = await queue.enqueue(c, "analyze", MINT, "basic")
            bad = await queue.enqueue(c, "nonsense", MINT, None)
        for _ in range(50):
            async with pool.acquire() as c:
                g = await queue.get(c, good.id)
                b = await queue.get(c, bad.id)
            if g and b and g.status == "done" and b.status == "failed":
                break
            await asyncio.sleep(0.1)
        assert g and g.status == "done" and g.result_version == 1
        assert b and b.status == "failed" and "unknown job kind" in (b.last_error or "")
        async with pool.acquire() as c:
            n = await c.fetchval("select count(*) from analysis where mint=$1", MINT)
        assert n == 1
    finally:
        w.request_stop()
        await asyncio.wait_for(task, timeout=5)


async def test_worker_stops_promptly_when_idle(pool: asyncpg.Pool, migrated_db: str) -> None:
    w = Worker(pool, _settings(migrated_db))
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.3)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    w.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert loop.time() - t0 < 1.0
