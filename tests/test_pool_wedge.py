"""The analyzer restarts itself when its shared HTTP client's pool stops handing out
connections (2026-11-09 to 11-12: every read failed on PoolTimeout for 2.5 days)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import needs_db
from tokensage import worker as worker_mod
from tokensage.config import Settings
from tokensage.db import create_pool
from tokensage.net import metrics
from tokensage.worker import Worker


@pytest.fixture(autouse=True)
def _fresh_health() -> None:
    metrics.pool_health.ok()


def test_pool_health_needs_both_time_and_count() -> None:
    h = metrics.PoolHealth()
    assert not h.wedged(min_s=120, min_timeouts=20, now=0.0)
    for _ in range(25):
        h.timed_out(now=0.0)
    assert not h.wedged(min_s=120, min_timeouts=20, now=60.0)
    assert h.wedged(min_s=120, min_timeouts=20, now=120.0)
    h2 = metrics.PoolHealth()
    for _ in range(5):
        h2.timed_out(now=0.0)
    assert not h2.wedged(min_s=120, min_timeouts=20, now=600.0)


def test_any_connection_ends_the_streak() -> None:
    h = metrics.PoolHealth()
    for _ in range(30):
        h.timed_out(now=0.0)
    h.ok()
    assert not h.wedged(min_s=120, min_timeouts=20, now=1_000.0)


async def test_metered_transport_notes_pool_timeouts() -> None:
    def answer(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/pool":
            raise httpx.PoolTimeout("no connection")
        if req.url.path == "/connect":
            raise httpx.ConnectTimeout("slow host")
        return httpx.Response(500)

    metrics.meter.take()
    transport = metrics.MeteredTransport(httpx.MockTransport(answer))
    async with httpx.AsyncClient(transport=transport) as c:
        for _ in range(3):
            with pytest.raises(httpx.PoolTimeout):
                await c.get("https://api.dexscreener.com/pool")
        assert metrics.pool_health.timeouts == 3
        # a 500 still had a connection: the pool is fine
        await c.get("https://api.dexscreener.com/ok")
        assert metrics.pool_health.timeouts == 0
        with pytest.raises(httpx.PoolTimeout):
            await c.get("https://api.dexscreener.com/pool")
        # so did a request whose own connect timed out
        with pytest.raises(httpx.ConnectTimeout):
            await c.get("https://api.dexscreener.com/connect")
        assert metrics.pool_health.timeouts == 0


async def test_rpc_host_counts_too() -> None:
    """The RPC host skips the transport's metering (SolanaRpc meters it) but not the health."""

    def answer(req: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("no connection")

    metrics.meter.configure(rpc_url="https://rpc.test/", ipfs_gateways=[])
    try:
        transport = metrics.MeteredTransport(httpx.MockTransport(answer))
        async with httpx.AsyncClient(transport=transport) as c:
            with pytest.raises(httpx.PoolTimeout):
                await c.post("https://rpc.test/", json={})
        assert metrics.pool_health.timeouts == 1
    finally:
        metrics.meter.configure(ipfs_gateways=[])


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
        solana_rpc_url="https://rpc.test/",
        ipfs_gateways="https://gw1.test,https://gw2.test",
        worker_poll_interval_s=0.1,
        worker_shutdown_grace_s=0.1,
        _env_file=None,  # type: ignore[call-arg]
    )


@needs_db
async def test_worker_stops_with_exit_code_on_a_wedged_pool(
    pool: asyncpg.Pool, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "POOL_WATCH_EVERY_S", 0.05)
    monkeypatch.setattr(worker_mod, "POOL_WEDGE_S", 0.0)
    monkeypatch.setattr(worker_mod, "POOL_WEDGE_MIN_TIMEOUTS", 3)
    w = Worker(pool, _settings(migrated_db), concurrency=1, exit_on_wedged_pool=True)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.2)
    assert not task.done()  # healthy pool: keeps running
    for _ in range(3):
        metrics.pool_health.timed_out()
    await asyncio.wait_for(task, timeout=5)
    assert w.exit_code == 1


@needs_db
async def test_inline_worker_never_stops_for_the_pool(
    pool: asyncpg.Pool, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "POOL_WATCH_EVERY_S", 0.05)
    monkeypatch.setattr(worker_mod, "POOL_WEDGE_S", 0.0)
    monkeypatch.setattr(worker_mod, "POOL_WEDGE_MIN_TIMEOUTS", 1)
    w = Worker(pool, _settings(migrated_db), concurrency=1)
    task = asyncio.create_task(w.run())
    try:
        metrics.pool_health.timed_out()
        await asyncio.sleep(0.3)
        assert not task.done()
        assert w.exit_code == 0
    finally:
        w.request_stop()
        await asyncio.wait_for(task, timeout=5)
