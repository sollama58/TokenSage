"""asyncpg pool helpers. Plain SQL everywhere; no ORM."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

from tokensage.config import get_settings

log = structlog.get_logger("db")
# Same reason as migrations/env.py: on a fresh deploy the database may still be starting.
CONNECT_WAIT_S = 120.0
CONNECT_RETRY_S = 3.0
_NOT_READY = (OSError, asyncpg.CannotConnectNowError, asyncio.TimeoutError)
# asyncpg prepares every statement, and after five runs Postgres may switch it to a generic
# plan that cannot see the time bounds: the same-name and current-meta lookups then scan the
# whole 30/90-day created_at index (tens of ms to seconds) instead of the name/trigram index.
# A custom plan per execution costs tens of microseconds.
SERVER_SETTINGS = {"plan_cache_mode": "force_custom_plan"}


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb <-> Python dict without manual json.dumps at every call site.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text"
    )


async def _reset_connection(conn: asyncpg.Connection) -> None:
    """Pool release hook replacing asyncpg's default reset query (pg_advisory_unlock_all,
    CLOSE ALL, UNLISTEN *, RESET ALL), which costs a round trip on every release. asyncpg
    still rolls back an open transaction and drops listener callbacks before calling this.
    Nothing here leaves session state behind: advisory locks are xact-scoped, there are no
    SET/DECLARE statements, and the LISTEN holders (queue.DoneWaiter, the worker) unlisten
    through remove_listener before releasing."""


async def create_pool(
    dsn: str | None = None,
    min_size: int = 1,
    max_size: int = 5,
    *,
    wait_s: float = CONNECT_WAIT_S,
) -> asyncpg.Pool:
    dsn = dsn or get_settings().database_url
    deadline = time.monotonic() + wait_s
    while True:
        try:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                init=_init_connection,
                reset=_reset_connection,
                command_timeout=30,
                server_settings=SERVER_SETTINGS,
            )
            assert pool is not None
            return pool
        except _NOT_READY as e:
            if time.monotonic() >= deadline:
                raise
            log.warning("db.not_ready", error=f"{type(e).__name__}: {e}", retry_s=CONNECT_RETRY_S)
            await asyncio.sleep(CONNECT_RETRY_S)


@asynccontextmanager
async def connection(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn:
        yield conn


def record_to_dict(rec: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(rec) if rec is not None else None
