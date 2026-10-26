"""Worker loop: processes jobs, survives a bad job, stops cleanly and releases work on stop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import CID_META, FakeChain, install_web, public_resolver
from tokensage import queue
from tokensage.config import Settings
from tokensage.db import create_pool
from tokensage.net import safe_fetch
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


@pytest.fixture(autouse=True)
def _fake_world(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    chain = FakeChain()
    chain.add_t22_pump(MINT, "Fear Of Missing Out", "FOMO", f"https://ipfs.io/ipfs/{CID_META}")
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        yield


def _settings(db: str) -> Settings:
    return Settings(
        database_url=db,
        solana_rpc_url="https://rpc.test/",
        ipfs_gateways="https://gw1.test,https://gw2.test",
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


async def test_worker_stops_a_job_over_its_time_cap_and_frees_the_slot(
    pool: asyncpg.Pool, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26: nine of twelve analyzer slots were held by jobs 'running' since 10-23, their
    leases renewed by the heartbeat. A job over job_max_s is failed, its slot runs the next job,
    and the failure names the stage it hung in."""
    from tokensage import analyzer
    from tokensage import worker as worker_mod

    real_analyze = worker_mod.analyze
    hung = asyncio.Event()

    async def analyze_or_hang(conn, ctx, mint, depth, **kw):  # type: ignore[no-untyped-def]
        if mint == "HANGMINT":
            analyzer._stage("metadata")
            hung.set()
            await asyncio.sleep(3600)
        return await real_analyze(conn, ctx, mint, depth, **kw)

    monkeypatch.setattr(worker_mod, "analyze", analyze_or_hang)
    settings = _settings(migrated_db)
    settings.job_max_s = 0.5
    settings.job_max_attempts = 1  # fail() marks it failed on the first strike
    w = Worker(pool, settings, concurrency=1)
    task = asyncio.create_task(w.run())
    try:
        async with pool.acquire() as c:
            stuck = await queue.enqueue(c, "analyze", "HANGMINT", "basic", priority=10)
            good = await queue.enqueue(c, "analyze", MINT, "basic")
        await asyncio.wait_for(hung.wait(), timeout=5)
        for _ in range(100):
            async with pool.acquire() as c:
                s = await queue.get(c, stuck.id)
                g = await queue.get(c, good.id)
            if s and g and s.status == "failed" and g.status == "done":
                break
            await asyncio.sleep(0.1)
        assert s and s.status == "failed", s
        assert "timed out after 0.5 s (stage: metadata)" in (s.last_error or ""), s.last_error
        # the one slot went on to the next job
        assert g and g.status == "done" and g.result_version == 1
        # the finished analysis recorded where its time went
        async with pool.acquire() as c:
            payload = await c.fetchval("select payload from job where id=$1", good.id)
        assert payload and set(payload["timing"]) >= {"resolve_ms", "engine_ms", "total_ms"}
        assert not w.active_jobs
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
