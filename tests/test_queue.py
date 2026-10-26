"""Queue semantics: single-flight, priority bump, leases, retry/fail, release."""

from __future__ import annotations

import json
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


async def test_batch_priority_bump_keeps_a_parked_retry_parked(conn: asyncpg.Connection) -> None:
    """A batch re-send at a higher priority raises the job but does not cut short a retry
    parked on a future run_after; only an interactive request does that."""
    j = await queue.enqueue(conn, "analyze", MINT, "basic", priority=queue.PRIORITY_BATCH_LOW)
    await conn.execute("update job set run_after = now() + interval '1 minute' where id=$1", j.id)
    await queue.enqueue(conn, "analyze", MINT, "basic", priority=queue.PRIORITY_BATCH_HIGH)
    row = await conn.fetchrow(
        "select priority, run_after > now() as parked from job where id=$1", j.id
    )
    assert row["priority"] == queue.PRIORITY_BATCH_HIGH and row["parked"]
    await queue.enqueue(conn, "analyze", MINT, "basic", priority=queue.PRIORITY_API)
    row = await conn.fetchrow(
        "select priority, run_after > now() as parked from job where id=$1", j.id
    )
    assert row["priority"] == queue.PRIORITY_API and not row["parked"]


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


async def test_complete_keeps_the_timing_under_the_payload(conn: asyncpg.Connection) -> None:
    j = await queue.enqueue(conn, "analyze", MINT, "basic")
    await conn.execute('update job set payload = \'{"hints": {"name": "x"}}\' where id=$1', j.id)
    claimed = await queue.claim(conn, 60)
    assert claimed is not None and claimed.id == j.id
    assert await queue.complete(
        conn, j.id, 1, claimed.lease_token, timing={"resolve_ms": 12, "total_ms": 340}
    )
    row = await conn.fetchrow("select payload::text as p from job where id=$1", j.id)
    assert row is not None
    payload = json.loads(row["p"])
    assert payload["hints"] == {"name": "x"}  # the hints stay
    assert payload["timing"] == {"resolve_ms": 12, "total_ms": 340}
    # without a timing the payload is left alone
    j2 = await queue.enqueue(conn, "analyze", "M2", "basic")
    c2 = await queue.claim(conn, 60)
    assert c2 is not None and c2.id == j2.id
    assert await queue.complete(conn, j2.id, 1, c2.lease_token)
    assert await conn.fetchval("select payload from job where id=$1", j2.id) is None


async def test_complete_notifies_waiter(migrated_db: str, conn: asyncpg.Connection) -> None:
    import asyncio

    from tokensage.db import create_pool

    pool = await create_pool(migrated_db, min_size=1, max_size=2)
    waiter = queue.DoneWaiter(pool)
    await waiter.start()
    try:
        j = await queue.enqueue(conn, "analyze", MINT, "full")
        assert await queue.claim(conn, lease_s=60)  # only a running job can be completed
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


async def test_callback_waits_for_its_target_and_is_released_on_completion(
    conn: asyncpg.Connection,
) -> None:
    from tokensage import callbacks
    from tokensage.db import _init_connection

    await _init_connection(conn)  # jsonb codec, as the app's pool installs it
    target = await queue.enqueue(conn, "analyze", MINT, "full")
    claimed = await queue.claim(conn, lease_s=60)
    assert claimed and claimed.id == target.id
    await callbacks.schedule(
        conn, target_job_id=target.id, callback_url="https://hook.test/x", key_digest="00"
    )
    # target still running: the callback is not claimable yet
    assert await queue.claim(conn, lease_s=60) is None
    await queue.complete(conn, target.id, result_version=1)
    cb = await queue.claim(conn, lease_s=60)
    assert cb is not None and cb.kind == "callback"
    # deferring gives the attempt back
    await queue.defer(conn, cb.id, 60)
    row = await conn.fetchrow("select status, attempts from job where id=$1", cb.id)
    assert row["status"] == "pending" and row["attempts"] == 0


async def test_callback_for_finished_target_is_runnable_at_once(conn: asyncpg.Connection) -> None:
    from tokensage import callbacks
    from tokensage.db import _init_connection

    await _init_connection(conn)  # jsonb codec, as the app's pool installs it
    target = await queue.enqueue(conn, "analyze", MINT, "full")
    await queue.claim(conn, lease_s=60)
    await queue.complete(conn, target.id, result_version=1)
    await callbacks.schedule(
        conn, target_job_id=target.id, callback_url="https://hook.test/x", key_digest="00"
    )
    cb = await queue.claim(conn, lease_s=60)
    assert cb is not None and cb.kind == "callback"


async def test_repeat_joins_park_one_callback_per_job_and_key(conn: asyncpg.Connection) -> None:
    """A key re-submitting a mint whose job is still open joins it; each join used to park one
    more callback row, without limit."""
    from tokensage import callbacks
    from tokensage.db import _init_connection

    await _init_connection(conn)
    target = await queue.enqueue(conn, "analyze", MINT, "full")
    for _ in range(5):
        await callbacks.schedule(
            conn, target_job_id=target.id, callback_url="https://hook.test/x", key_digest="00"
        )
    await callbacks.schedule(
        conn, target_job_id=target.id, callback_url="https://hook.test/x", key_digest="11"
    )
    n = await conn.fetchval("select count(*) from job where kind = 'callback'")
    assert n == 2


async def test_pending_count_ignores_jobs_parked_on_a_future_run_after(
    conn: asyncpg.Connection,
) -> None:
    """A metadata retry scheduled for later is not queue depth: nothing waits on a worker
    for it, so it must not count toward the 503 back-pressure limit or the admin alert."""
    await queue.enqueue(conn, "analyze", "M1", "basic")
    await conn.execute(
        """insert into job (kind, mint, depth, priority, run_after)
           values ('retry_metadata', 'M2', 'basic', $1, now() + interval '20 minutes')""",
        queue.PRIORITY_BACKGROUND,
    )
    assert await queue.pending_count(conn) == 1


async def test_low_tier_is_claimed_newest_first(conn: asyncpg.Connection) -> None:
    """Launch reads (PRIORITY_BATCH_LOW) go newest first under a backlog; the other tiers
    stay oldest first, and every tier still yields to a more urgent one."""
    mints = [f"{MINT[:-5]}low{i:02d}" for i in range(3)]
    low = []
    for i, m in enumerate(mints):
        j = await queue.enqueue(conn, "analyze", m, "basic", priority=queue.PRIORITY_BATCH_LOW)
        await conn.execute(
            "update job set run_after = now() - make_interval(secs => $2) where id = $1",
            j.id,
            float(60 * (3 - i)),
        )
        low.append(j.id)
    normal_a = await queue.enqueue(conn, "analyze", mints[0], "full", priority=queue.PRIORITY_BATCH)
    normal_b = await queue.enqueue(conn, "analyze", mints[1], "full", priority=queue.PRIORITY_BATCH)
    order = []
    while (j := await queue.claim(conn, lease_s=60)) is not None:
        order.append(j.id)
    assert order == [normal_a.id, normal_b.id, *reversed(low)]


async def test_expire_stale_low_drops_only_old_launch_reads(conn: asyncpg.Connection) -> None:
    old = await queue.enqueue(conn, "analyze", MINT, "basic", priority=queue.PRIORITY_BATCH_LOW)
    fresh = await queue.enqueue(conn, "analyze", MINT, "full", priority=queue.PRIORITY_BATCH_LOW)
    normal = await queue.enqueue(conn, "analyze", MINT[:-1] + "x", "basic", priority=50)
    hooked = await queue.enqueue(
        conn, "analyze", MINT[:-1] + "y", "basic", priority=queue.PRIORITY_BATCH_LOW
    )
    await conn.execute(
        "update job set run_after = now() - interval '20 minutes' where id = any($1::bigint[])",
        [old.id, normal.id, hooked.id],
    )
    await conn.execute(
        """insert into job (kind, priority, payload)
           values ('callback', 100, jsonb_build_object('target_job_id', $1::bigint))""",
        hooked.id,
    )
    assert await queue.expire_stale_low(conn, 900) == 1
    left = {r["id"] for r in await conn.fetch("select id from job where kind = 'analyze'")}
    assert left == {fresh.id, normal.id, hooked.id}


async def test_pending_count_by_priority_ignores_the_low_backlog(conn: asyncpg.Connection) -> None:
    for i in range(3):
        await queue.enqueue(
            conn, "analyze", f"{MINT[:-3]}b{i:02d}", "basic", priority=queue.PRIORITY_BATCH_LOW
        )
    await queue.enqueue(conn, "analyze", MINT, "full", priority=queue.PRIORITY_BATCH)
    assert await queue.pending_count(conn) == 4
    assert await queue.pending_count(conn, queue.PRIORITY_BATCH) == 1
    assert await queue.pending_count(conn, queue.PRIORITY_API) == 0
    assert await queue.pending_count(conn, queue.PRIORITY_BATCH_LOW) == 4


async def test_hints_join_an_open_job_from_its_own_key_only(conn: asyncpg.Connection) -> None:
    """Audit 2026-10-22 TS-4: another key's hints filled a hint-less open job, and the read
    built from them was served to the job's owner (SG-003's cross-key poisoning)."""
    from tokensage.db import _init_connection

    await _init_connection(conn)  # json codecs, as the API pool has
    hints = {"hints": {"description": "poison"}}
    j = await queue.enqueue(conn, "analyze", MINT, "basic", requested_by="a")
    await queue.enqueue(conn, "analyze", MINT, "basic", requested_by="b", payload=hints)
    assert await conn.fetchval("select payload from job where id=$1", j.id) is None
    own = {"hints": {"description": "mine"}}
    await queue.enqueue(conn, "analyze", MINT, "basic", requested_by="a", payload=own)
    assert await conn.fetchval("select payload from job where id=$1", j.id) == own
