"""The hourly maintenance job: the analysis prune keeps the newest 3 versions per mint and
depth without sweeping the whole table, and one failing step does not stop the others."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from tests.conftest import needs_db
from tokensage.jobs import maintenance


@pytest.fixture
async def db(
    migrated_db: str, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[asyncpg.Connection]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    maintenance.get_settings.cache_clear()
    conn = await asyncpg.connect(migrated_db)
    try:
        yield conn
    finally:
        await conn.close()
        maintenance.get_settings.cache_clear()


async def _analysis(
    conn: asyncpg.Connection, mint: str, version: int, depth: str, age: str
) -> None:
    await conn.execute("insert into token (mint) values ($1) on conflict do nothing", mint)
    await conn.execute(
        """insert into analysis (mint, version, depth, doc, categories, flags, created_at)
           values ($1, $2, $3, '{}'::jsonb, '{}', '{}', now() - $4::text::interval)""",
        mint,
        version,
        depth,
        age,
    )


async def _versions(conn: asyncpg.Connection, mint: str) -> list[int]:
    rows = await conn.fetch("select version from analysis where mint=$1 order by version", mint)
    return [r["version"] for r in rows]


@needs_db
async def test_prune_keeps_newest_three_per_depth(db: asyncpg.Connection) -> None:
    # re-analysed today: v1 and v2 are past 30 days and superseded three times over
    for v, age in [(1, "60 days"), (2, "45 days"), (3, "40 days"), (4, "2 hours"), (5, "1 hour")]:
        await _analysis(db, "again", v, "basic", age)
    await _analysis(db, "again", 6, "full", "50 days")  # the only full read: kept
    # just crossed 30 days, superseded by three newer versions
    for v, age in [(1, "31 days"), (2, "20 days"), (3, "10 days"), (4, "5 days")]:
        await _analysis(db, "aged", v, "basic", age)
    # old and superseded, but not in either window: left for disk's sake, not swept
    for v, age in [(1, "90 days"), (2, "80 days"), (3, "70 days"), (4, "60 days")]:
        await _analysis(db, "quiet", v, "basic", age)
    # old but only two versions: kept
    await _analysis(db, "few", 1, "basic", "60 days")
    await _analysis(db, "few", 2, "basic", "1 hour")

    assert await maintenance._prune_analyses(db) == 3
    assert await _versions(db, "again") == [3, 4, 5, 6]
    assert await _versions(db, "aged") == [2, 3, 4]
    assert await _versions(db, "quiet") == [1, 2, 3, 4]
    assert await _versions(db, "few") == [1, 2]
    assert await maintenance._prune_analyses(db) == 0


@needs_db
async def test_failing_step_does_not_stop_the_rest(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(conn: asyncpg.Connection) -> Any:
        raise TimeoutError

    ran: list[str] = []

    async def usage(conn: asyncpg.Connection) -> int:
        ran.append("usage")
        return 0

    steps = dict((k, (f, req)) for k, f, req in maintenance.STEPS)
    steps["recall"] = (boom, False)
    steps["pruned_usage_rows"] = (usage, True)
    monkeypatch.setattr(maintenance, "STEPS", [(k, f, req) for k, (f, req) in steps.items()])

    stats = await maintenance.run_once()
    assert ran == ["usage"]
    assert stats["failed_steps"] == []  # recall is reporting only: logged, run stays green
    assert stats["pruned_analyses"] == 0

    steps["pruned_jobs"] = (boom, True)
    monkeypatch.setattr(maintenance, "STEPS", [(k, f, req) for k, (f, req) in steps.items()])
    stats = await maintenance.run_once()
    assert stats["failed_steps"] == ["pruned_jobs"]
    assert "pruned_usage_rows" in stats  # later steps still ran
