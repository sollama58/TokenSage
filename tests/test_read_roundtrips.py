"""The quick-read path's database round trips: the depth filter in SQL keeps the old
"newest of the 5 latest versions at this depth or deeper" rule, and the pool's release hook
leaves no transaction behind."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from tests.conftest import needs_db
from tokensage.api import service, usage
from tokensage.db import create_pool

pytestmark = needs_db


async def _seed(conn: asyncpg.Connection, mint: str, depths: list[str]) -> None:
    await conn.execute(
        "insert into token (mint, created_at) values ($1, $2)",
        mint,
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    for v, d in enumerate(depths, start=1):
        await conn.execute(
            "insert into analysis (mint, version, depth, doc) values ($1, $2, $3, $4)",
            mint,
            v,
            d,
            {"v": v, "depth": d},
        )


async def test_latest_analysis_depth_rule(migrated_db: str, clean_tables: None) -> None:
    pool = await create_pool(migrated_db, max_size=2)
    try:
        async with pool.acquire() as conn:
            # versions 1..7, oldest first; only version 2 is full
            await _seed(conn, "M1", ["basic", "full", "basic", "basic", "basic", "basic", "basic"])
            await _seed(conn, "M2", ["basic", "basic", "full", "basic", "basic"])
            got = await service.latest_analysis(conn, "M1", "basic")
            assert got is not None and got[1] == 7 and got[0] == {"v": 7, "depth": "basic"}
            # the only full version is older than the 5 newest: not looked at (as before)
            assert await service.latest_analysis(conn, "M1", "full") is None
            got = await service.latest_analysis(conn, "M2", "full")
            assert got is not None and got[1] == 3
            assert await service.latest_analysis(conn, "nope", "basic") is None
            created, latest = await service._created_and_latest(conn, "M2", "full")
            assert created == datetime(2026, 1, 1, tzinfo=UTC)
            assert latest is not None and latest[1] == 3
            assert await service._created_and_latest(conn, "nope", "basic") == (None, None)
    finally:
        await pool.close()


async def test_released_connection_has_no_open_transaction(
    migrated_db: str, clean_tables: None
) -> None:
    pool = await create_pool(migrated_db, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            await usage.bump(conn, "k", requests=1)
            # released without commit or rollback
        async with pool.acquire() as conn:
            assert not conn.is_in_transaction()
            assert await usage.today(conn, "k") == usage.Usage()
            u = await usage.bump(conn, "k", requests=2, full=1)
            assert u == usage.Usage(requests=2, full_calls=1, refreshes=0)
    finally:
        await pool.close()
