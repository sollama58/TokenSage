"""asyncpg pool helpers. Plain SQL everywhere; no ORM."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from tokensage.config import get_settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb <-> Python dict without manual json.dumps at every call site.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text"
    )


async def create_pool(dsn: str | None = None, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    dsn = dsn or get_settings().database_url
    pool = await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=_init_connection, command_timeout=30
    )
    assert pool is not None
    return pool


@asynccontextmanager
async def connection(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as conn:
        yield conn


def record_to_dict(rec: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(rec) if rec is not None else None
