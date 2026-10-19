"""Regression tests for the 2026-10-19 audit: each reproduces a verified bug."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tests.conftest import needs_db
from tokensage.resolve import metadata as md


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    await conn.execute("truncate token_metadata, image cascade")
    try:
        yield conn
    finally:
        await conn.close()


# ------------------------------------------------- non-finite numbers in metadata JSON


HOSTILE_NUMBERS = (
    b'{"name": "Nan Coin", "symbol": "NAN", "a": NaN, "b": Infinity, "c": -Infinity,'
    b' "d": 1e400, "e": [1.5, NaN], "f": {"g": -1e999}, "h": 2.5, "i": 7}'
)


def test_non_finite_numbers_read_as_null() -> None:
    m = md.build("https://u.test/m", HOSTILE_NUMBERS)
    assert m.status == "ok" and m.name == "Nan Coin"
    assert m.raw is not None
    assert m.raw["a"] is None and m.raw["b"] is None and m.raw["c"] is None
    assert m.raw["d"] is None and m.raw["e"] == [1.5, None] and m.raw["f"] == {"g": None}
    assert m.raw["h"] == 2.5 and m.raw["i"] == 7  # ordinary numbers are untouched


@needs_db
async def test_metadata_with_non_finite_numbers_can_be_stored(db: asyncpg.Connection) -> None:
    """Python's json reads NaN/Infinity, Postgres jsonb refuses them: the metadata write
    failed on every attempt, so a launcher could make its coin unanalysable."""
    m = md.build("https://u.test/m", HOSTILE_NUMBERS)
    await db.execute("insert into token (mint) values ('m')")
    await md.persist(db, "m", m, 0)
    raw = await db.fetchval("select raw from token_metadata where mint='m'")
    assert raw["a"] is None and raw["h"] == 2.5


# ------------------------------------------------- /v1/jobs answers analysis jobs only


@needs_db
async def test_jobs_route_hides_internal_jobs(client: httpx.AsyncClient, migrated_db: str) -> None:
    """A callback job's error names the address its URL resolved to at delivery; job ids are
    sequential, so any key could read another key's (the batch route keeps that detail out of
    its own answer since #71)."""
    conn = await asyncpg.connect(migrated_db)
    try:
        cb = await conn.fetchval(
            """insert into job (kind, mint, depth, status, last_error, finished_at)
               values ('callback', null, null, 'failed',
                       'callback url rejected: hook.test resolves to non-public 10.1.2.3', now())
               returning id"""
        )
        now_job = await conn.fetchval(
            """insert into job (kind, mint, depth, status, finished_at)
               values ('now', 'So11111111111111111111111111111111111111112', 'full', 'done',
                       now()) returning id"""
        )
        an = await conn.fetchval(
            """insert into job (kind, mint, depth, status, last_error, finished_at)
               values ('analyze', 'So11111111111111111111111111111111111111112', 'basic',
                       'failed', 'boom', now()) returning id"""
        )
    finally:
        await conn.close()
    for jid in (cb, now_job):
        r = await client.get(f"/v1/jobs/{jid}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found"
        assert "10.1.2.3" not in r.text
    r = await client.get(f"/v1/jobs/{an}")
    assert r.status_code == 200 and r.json()["status"] == "failed"
