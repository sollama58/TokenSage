"""Referent recall (guide §5.9): status buckets and the per-depth summary the maintenance
job logs."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.conftest import needs_db
from tokensage import recall


def test_status_buckets() -> None:
    assert recall.status(None) == "none"
    assert recall.status(0.2) == "guess"
    assert recall.status(0.44) == "guess"
    assert recall.status(0.45) == "weak"
    assert recall.status(0.59) == "weak"
    assert recall.status(0.6) == "resolved"
    assert recall.status(0.97) == "resolved"


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def _analysis(
    conn: asyncpg.Connection, mint: str, version: int, depth: str, score: float | None
) -> None:
    await conn.execute("insert into token (mint) values ($1) on conflict do nothing", mint)
    await conn.execute(
        """insert into analysis (mint, version, depth, doc, referent, referent_score,
                                 categories, flags)
           values ($1, $2, $3, '{}'::jsonb, $4, $5, '{}', '{}')""",
        mint,
        version,
        depth,
        "something" if score is not None else None,
        score,
    )


@needs_db
async def test_summary_counts_latest_version_per_mint_and_depth(db: asyncpg.Connection) -> None:
    await db.execute("truncate analysis, token cascade")
    await _analysis(db, "m1", 1, "basic", None)  # superseded by v2 below: not counted
    await _analysis(db, "m1", 2, "basic", 0.9)
    await _analysis(db, "m2", 1, "basic", 0.5)
    await _analysis(db, "m3", 1, "basic", 0.3)
    await _analysis(db, "m4", 1, "basic", None)
    await _analysis(db, "m5", 1, "full", 0.7)
    s = await recall.summary(db)
    assert s["hours"] == 24
    assert s["depths"]["basic"] == {
        "total": 4,
        "none": 1,
        "guess": 1,
        "weak": 1,
        "resolved": 1,
        "resolved_share": 0.25,
    }
    assert s["depths"]["full"]["resolved"] == 1
    assert s["depths"]["full"]["resolved_share"] == 1.0


@needs_db
async def test_summary_ignores_old_rows(db: asyncpg.Connection) -> None:
    await db.execute("truncate analysis, token cascade")
    await _analysis(db, "old", 1, "basic", 0.9)
    await db.execute("update analysis set created_at = now() - interval '2 days'")
    s = await recall.summary(db)
    assert s["depths"] == {}
