"""The analyzer runs several jobs at once, so slow upstreams don't serialise it."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import CID_META, RPC, FakeChain, install_web, public_resolver
from tests.test_worker import _settings
from tokensage import queue
from tokensage.db import create_pool
from tokensage.net import safe_fetch
from tokensage.resolve.pump_event import b58encode
from tokensage.worker import Worker

pytestmark = needs_db


@pytest.fixture
async def pool(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(migrated_db, min_size=1, max_size=8)
    try:
        yield p
    finally:
        await p.close()


async def test_worker_runs_jobs_concurrently(
    pool: asyncpg.Pool, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow upstreams must not serialise the analyzer: N loops keep N jobs in flight."""
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    rnd = random.Random(7)
    mints = [b58encode(bytes(rnd.getrandbits(8) for _ in range(32))) for _ in range(4)]
    chain = FakeChain()
    for m in mints:
        chain.add_t22_pump(m, "Fear Of Missing Out", "FOMO", f"https://ipfs.io/ipfs/{CID_META}")

    async def slow_rpc(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.25)  # every RPC call is slow, like a busy provider
        return chain.handle(request)

    with respx.mock(assert_all_called=False) as router:
        router.post(RPC).mock(side_effect=slow_rpc)  # first match wins over install_web's
        install_web(router, chain)
        async with pool.acquire() as c:
            ids = [(await queue.enqueue(c, "analyze", m, "basic")).id for m in mints]
        w = Worker(pool, _settings(migrated_db), concurrency=4)
        in_flight = 0
        peak = 0
        process = w._process

        async def counting(conn: asyncpg.Connection, job: queue.Job) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await process(conn, job)
            finally:
                in_flight -= 1

        w._process = counting  # type: ignore[method-assign]
        task = asyncio.create_task(w.run())
        try:
            for _ in range(200):
                async with pool.acquire() as c:
                    done = await c.fetchval(
                        "select count(*) from job where id = any($1) and status='done'", ids
                    )
                if done == len(ids):
                    break
                await asyncio.sleep(0.05)
            assert done == len(ids)
            assert peak == len(ids), f"only {peak} job(s) ran at once"
        finally:
            w.request_stop()
            await asyncio.wait_for(task, timeout=10)
