"""Queue semantics: single-flight, priority bump, leases, retry/fail, release."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.conftest import needs_db
from tokensage import queue

pytestmark = needs_db
MINT = "457V2vvjqXTMFzivq9tvBqhDaxfke2523hHDB6brpump"


@pytest.fixture
async def conn(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_db)
    try:
        yield c
    finally:
        await c.close()


async def test_enqueue_is_single_flight_and_bumps_priority(conn: asyncpg.Connection) -> None:
    a = await queue.enqueue(conn, "analyze", MINT, "full", priority=100)
    b = await queue.enqueue(conn, "analyze", MINT, "full", priority=10)
    assert a.id == b.id
    assert await conn.fetchval("select priority from job where id=$1", a.id) == 10
    c = await queue.enqueue(conn, "analyze", MINT, "basic")
    assert c.id != a.id


async def test_claim_orders_by_priority_and_respects_lease(conn: asyncpg.Connection) -> None:
    low = await queue.enqueue(conn, "analyze", MINT, "basic", priority=100)
    high = await queue.enqueue(conn, "analyze", MINT, "full", priority=10)
    first = await queue.claim(conn, lease_s=60)
    assert first and first.id == high.id and first.status == "running"
    second = await queue.claim(conn, lease_s=60)
    assert second and second.id == low.id
    assert await queue.claim(conn, lease_s=60) is None  # both leased

    # expire the lease -> claimable again, attempts incremented
    await conn.execute(
        "update job set locked_until = now() - interval '1 second' where id=$1", high.id
    )
    again = await queue.claim(conn, lease_s=60)
    assert again and again.id == high.id and again.attempts == 2


async def test_fail_retries_then_fails(conn: asyncpg.Connection) -> None:
    j = await queue.enqueue(conn, "analyze", MINT, "full")
    claimed = await queue.claim(conn, lease_s=60)
    assert claimed and claimed.attempts == 1
    await queue.fail(conn, j.id, "boom", max_attempts=2, retry_in_s=0)
    assert await conn.fetchval("select status from job where id=$1", j.id) == "pending"
    claimed = await queue.claim(conn, lease_s=60)
    assert claimed and claimed.attempts == 2
    await queue.fail(conn, j.id, "boom again", max_attempts=2, retry_in_s=0)
    row = await conn.fetchrow("select status, last_error, finished_at from job where id=$1", j.id)
    assert row and row["status"] == "failed" and row["last_error"] == "boom again"
    assert row["finished_at"] is not None
    # a failed job no longer blocks a fresh enqueue for the same key
    j2 = await queue.enqueue(conn, "analyze", MINT, "full")
    assert j2.id != j.id


async def test_release_returns_job_untouched(conn: asyncpg.Connection) -> None:
    j = await queue.enqueue(conn, "analyze", MINT, "full")
    await queue.claim(conn, lease_s=60)
    await queue.release(conn, j.id)
    row = await conn.fetchrow("select status, attempts, locked_until from job where id=$1", j.id)
    assert row and row["status"] == "pending" and row["attempts"] == 0
    assert row["locked_until"] is None


async def test_complete_notifies_waiter(migrated_db: str, conn: asyncpg.Connection) -> None:
    import asyncio

    from tokensage.db import create_pool

    pool = await create_pool(migrated_db, min_size=1, max_size=2)
    waiter = queue.DoneWaiter(pool)
    await waiter.start()
    try:
        j = await queue.enqueue(conn, "analyze", MINT, "full")
        task = asyncio.create_task(waiter.wait(j.id, timeout_s=5))
        await asyncio.sleep(0.1)
        await queue.complete(conn, j.id, result_version=1)
        assert await task is True
        # already-finished job returns immediately
        assert await waiter.wait(j.id, timeout_s=1) is True
        # unknown job times out
        assert await waiter.wait(999_999, timeout_s=0.3) is False
    finally:
        await waiter.stop()
        await pool.close()


async def test_enqueue_reuses_a_just_finished_job(conn: asyncpg.Connection) -> None:
    j = await queue.enqueue(conn, "analyze", MINT, "full")
    claimed = await queue.claim(conn, lease_s=60)
    assert claimed and claimed.id == j.id
    await queue.complete(conn, j.id, result_version=1)
    # a caller that missed the cache while the job committed gets the finished job back
    again = await queue.enqueue(conn, "analyze", MINT, "full", reuse_done_within_s=30)
    assert again.id == j.id and again.status == "done"
    # other depths, refreshes (no reuse window) and stale finishes create a new job
    other = await queue.enqueue(conn, "analyze", MINT, "basic", reuse_done_within_s=30)
    assert other.id != j.id and other.status == "pending"
    fresh = await queue.enqueue(conn, "analyze", MINT, "full")
    assert fresh.id != j.id and fresh.status == "pending"
    await conn.execute("update job set finished_at = now() - interval '1 hour' where id=$1", j.id)
    await conn.execute("delete from job where id=$1", fresh.id)
    stale = await queue.enqueue(conn, "analyze", MINT, "full", reuse_done_within_s=30)
    assert stale.id not in (j.id, fresh.id) and stale.status == "pending"
