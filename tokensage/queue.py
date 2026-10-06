"""Postgres-backed job queue: single-flight enqueue, leased claims, LISTEN/NOTIFY wakeups.

Workers and the API only ever talk through this table (Render workers have no inbound
network). Writes are idempotent so overlapping deploys are harmless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

CHANNEL_NEW = "job_new"
CHANNEL_DONE = "job_done"

PRIORITY_API = 10  # interactive requests jump the queue
PRIORITY_BATCH = 50
PRIORITY_BACKGROUND = 100


@dataclass
class Job:
    id: int
    kind: str
    mint: str | None
    depth: str | None
    status: str
    attempts: int
    result_version: int | None
    last_error: str | None
    error_code: str | None = None
    payload: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, r: asyncpg.Record) -> Job:
        return cls(
            id=r["id"],
            kind=r["kind"],
            mint=r["mint"],
            depth=r["depth"],
            status=r["status"],
            attempts=r["attempts"],
            result_version=r["result_version"],
            last_error=r["last_error"],
            error_code=r["error_code"],
            payload=r["payload"] if "payload" in r.keys() else None,
        )


async def enqueue(
    conn: asyncpg.Connection,
    kind: str,
    mint: str | None,
    depth: str | None,
    priority: int = PRIORITY_BACKGROUND,
    requested_by: str | None = None,
    reuse_done_within_s: float = 0,
) -> Job:
    """Insert a job, or return the open one for (kind, mint, depth). Raises the priority
    of an existing job if the new request is more urgent.

    With reuse_done_within_s > 0, an identical job that finished that recently is returned
    instead of a new one. This closes the race where a caller misses the cache just before
    the running job commits its result and would otherwise enqueue a duplicate."""
    async with conn.transaction():
        # Serialise against complete() for the same key, so a caller sees the job either
        # still open (and joins it) or finished (and reuses it), never the commit in between.
        await conn.execute("select pg_advisory_xact_lock($1)", _job_lock_key(kind, mint, depth))
        row = await _enqueue_row(
            conn, kind, mint, depth, priority, requested_by, reuse_done_within_s
        )
    job = Job.from_record(row)
    if job.status != "done":
        await conn.execute("select pg_notify($1, $2)", CHANNEL_NEW, str(job.id))
    return job


def _job_lock_key(kind: str, mint: str | None, depth: str | None) -> int:
    digest = hashlib.blake2b(f"{kind}|{mint}|{depth}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _enqueue_row(
    conn: asyncpg.Connection,
    kind: str,
    mint: str | None,
    depth: str | None,
    priority: int,
    requested_by: str | None,
    reuse_done_within_s: float,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        """
        with recent as (
          select * from job
          where $6 > 0 and kind = $1 and mint is not distinct from $2
            and depth is not distinct from $3 and status = 'done'
            and finished_at > now() - make_interval(secs => $6)
          order by finished_at desc
          limit 1
        ), ins as (
          insert into job (kind, mint, depth, priority, requested_by)
          select $1, $2, $3, $4, $5
          where not exists (select 1 from recent)
          on conflict (kind, mint, depth) where status in ('pending','running')
          do update set priority = least(job.priority, excluded.priority)
          returning *
        )
        select * from ins
        union all
        select * from recent
        limit 1
        """,
        kind,
        mint,
        depth,
        priority,
        requested_by,
        float(reuse_done_within_s),
    )
    assert row is not None
    return row


async def get(conn: asyncpg.Connection, job_id: int) -> Job | None:
    row = await conn.fetchrow("select * from job where id = $1", job_id)
    return Job.from_record(row) if row else None


async def pending_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("select count(*) from job where status = 'pending'")


async def claim(
    conn: asyncpg.Connection, lease_s: int, kinds: list[str] | None = None
) -> Job | None:
    """Atomically claim the most urgent runnable job. Expired leases are re-claimable."""
    row = await conn.fetchrow(
        """
        with next_job as (
          select id from job
          where (status = 'pending' or (status = 'running' and locked_until < now()))
            and run_after <= now()
            and ($2::text[] is null or kind = any($2))
          order by priority, run_after
          for update skip locked
          limit 1
        )
        update job set status = 'running', attempts = attempts + 1,
               locked_until = now() + make_interval(secs => $1)
        from next_job where job.id = next_job.id
        returning job.*
        """,
        lease_s,
        kinds,
    )
    return Job.from_record(row) if row else None


async def complete(conn: asyncpg.Connection, job_id: int, result_version: int | None) -> None:
    async with conn.transaction():
        key = await conn.fetchrow("select kind, mint, depth from job where id=$1", job_id)
        if key is not None:
            await conn.execute(
                "select pg_advisory_xact_lock($1)",
                _job_lock_key(key["kind"], key["mint"], key["depth"]),
            )
        await conn.execute(
            """update job set status='done', finished_at=now(), locked_until=null,
               result_version=$2, last_error=null where id=$1""",
            job_id,
            result_version,
        )
    await conn.execute("select pg_notify($1, $2)", CHANNEL_DONE, str(job_id))


async def fail(
    conn: asyncpg.Connection,
    job_id: int,
    error: str,
    max_attempts: int,
    retry_in_s: int = 30,
    error_code: str | None = None,
) -> None:
    """Retry with a delay until max_attempts, then mark failed. An error_code marks a
    definitive failure: no retry, and the API maps the code to a status."""
    await conn.execute(
        """
        update job set
          status = case when attempts >= $3 or $5::text is not null
                        then 'failed' else 'pending' end,
          run_after = now() + make_interval(secs => $4),
          locked_until = null,
          last_error = left($2, 2000),
          error_code = $5,
          finished_at = case when attempts >= $3 or $5::text is not null then now() else null end
        where id = $1
        """,
        job_id,
        error,
        max_attempts,
        retry_in_s,
        error_code,
    )
    await conn.execute("select pg_notify($1, $2)", CHANNEL_DONE, str(job_id))


async def release(conn: asyncpg.Connection, job_id: int) -> None:
    """Hand a claimed job back untouched (used on SIGTERM)."""
    await conn.execute(
        """update job set status='pending', attempts=greatest(attempts-1,0), locked_until=null
           where id=$1 and status='running'""",
        job_id,
    )


async def requeue_expired(conn: asyncpg.Connection) -> int:
    """Maintenance: return jobs whose lease expired to pending (the claim query already
    treats them as runnable; this keeps status honest for /readyz)."""
    res = await conn.execute(
        """update job set status='pending', locked_until=null
           where status='running' and locked_until < now()"""
    )
    return int(res.split()[-1])


class DoneWaiter:
    """One LISTEN connection shared by all in-flight API requests; wakes waiters by job id."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._conn: asyncpg.Connection | None = None
        self._waiters: dict[int, set[asyncio.Future[None]]] = {}

    async def start(self) -> None:
        self._conn = await self._pool.acquire()
        await self._conn.add_listener(CHANNEL_DONE, self._on_notify)

    async def stop(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.remove_listener(CHANNEL_DONE, self._on_notify)
            finally:
                await self._pool.release(self._conn)
                self._conn = None

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            job_id = int(payload)
        except ValueError:
            return
        for fut in self._waiters.pop(job_id, set()):
            if not fut.done():
                fut.set_result(None)

    async def wait(self, job_id: int, timeout_s: float) -> bool:
        """True if the job finished within the timeout (by notify or by polling fallback)."""
        if timeout_s <= 0:
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.setdefault(job_id, set()).add(fut)
        try:
            # Guard against a notify that fired before we subscribed.
            async with self._pool.acquire() as c:
                st = await c.fetchval("select status from job where id=$1", job_id)
            if st in ("done", "failed"):
                return True
            await asyncio.wait_for(fut, timeout=timeout_s)
            return True
        except TimeoutError:
            return False
        finally:
            s = self._waiters.get(job_id)
            if s is not None:
                s.discard(fut)
                if not s:
                    self._waiters.pop(job_id, None)


def utcnow() -> datetime:
    return datetime.now(UTC)


def seconds_from_now(s: int) -> datetime:
    return utcnow() + timedelta(seconds=s)


def dumps(o: Any) -> str:
    return json.dumps(o, default=str)
