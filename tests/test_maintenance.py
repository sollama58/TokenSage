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


async def _seed(conn: asyncpg.Connection) -> None:
    # the cursors as a deploy long after these rows would leave them: new rows from a day
    # ago, aged rows from the start of the table
    await conn.execute(
        """update maintenance_cursor set at = case name
             when 'analysis_new' then now() - interval '1 day'
             else now() - interval '1000 days' end"""
    )
    # re-analysed today: v1 and v2 are past 30 days and superseded three times over
    for v, age in [(1, "60 days"), (2, "45 days"), (3, "40 days"), (4, "2 hours"), (5, "1 hour")]:
        await _analysis(conn, "again", v, "basic", age)
    await _analysis(conn, "again", 6, "full", "50 days")  # the only full read: kept
    # crossed 30 days after its third newer version
    for v, age in [(1, "31 days"), (2, "20 days"), (3, "10 days"), (4, "5 days")]:
        await _analysis(conn, "aged", v, "basic", age)
    # long superseded: the aged cursor walks the backlog from the oldest row
    for v, age in [(1, "90 days"), (2, "80 days"), (3, "70 days"), (4, "60 days")]:
        await _analysis(conn, "quiet", v, "basic", age)
    # old but only two versions: kept
    await _analysis(conn, "few", 1, "basic", "60 days")
    await _analysis(conn, "few", 2, "basic", "1 hour")
    # superseded today but not yet 30 days old: kept
    for v, age in [(1, "20 days"), (2, "3 hours"), (3, "2 hours"), (4, "1 hour")]:
        await _analysis(conn, "young", v, "basic", age)


async def _check(conn: asyncpg.Connection) -> None:
    assert await _versions(conn, "again") == [3, 4, 5, 6]
    assert await _versions(conn, "aged") == [2, 3, 4]
    assert await _versions(conn, "quiet") == [2, 3, 4]
    assert await _versions(conn, "few") == [1, 2]
    assert await _versions(conn, "young") == [1, 2, 3, 4]


@needs_db
async def test_prune_keeps_newest_three_per_depth(db: asyncpg.Connection) -> None:
    await _seed(db)
    first = await maintenance._prune_analyses(db)
    assert first["deleted"] == 4
    assert first["new_lag_s"] is not None and first["new_lag_s"] < 3700 + 300
    assert first["aged_lag_s"] is not None and first["aged_lag_s"] < 3 * 86400
    await _check(db)
    assert (await maintenance._prune_analyses(db))["deleted"] == 0


@needs_db
async def test_prune_resumes_in_small_batches(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(db)
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 2)
    monkeypatch.setattr(maintenance, "PRUNE_BUDGET_S", 0.0)  # one batch per pass per run
    runs, deleted = 0, 0
    while True:
        r = await maintenance._prune_analyses(db)
        runs += 1
        deleted += r["deleted"]
        if r["batches"] == 0:
            break
        assert runs < 50
    assert runs > 2  # the work really was spread over several runs
    assert deleted == 4
    await _check(db)


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
    assert stats["pruned_analyses"]["deleted"] == 0

    steps["pruned_jobs"] = (boom, True)
    monkeypatch.setattr(maintenance, "STEPS", [(k, f, req) for k, (f, req) in steps.items()])
    stats = await maintenance.run_once()
    assert stats["failed_steps"] == ["pruned_jobs"]
    assert "pruned_usage_rows" in stats  # later steps still ran


@needs_db
async def test_prune_lookup_cache_drops_expired_lookups_only(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 2)  # several batches in one run
    await db.execute("truncate lookup_cache")
    rows = [
        ("bsky:q:old one", "9 days"),
        ("gnews:q:old two", "20 days"),
        ("gnews:old three", "30 days"),
        ("wiki:old four", "8 days 1 hour"),
        ("wiki:fresh", "6 days"),  # within WIKI_TTL: still served
        ("bsky:q:fresh", "1 hour"),
        ("gtrends:seen", "60 days"),  # long-lived trend rows are never pruned
        ("xtrends:seen", "60 days"),
    ]
    for key, age in rows:
        await db.execute(
            """insert into lookup_cache (key, value, fetched_at)
               values ($1, '[]'::jsonb, now() - $2::text::interval)""",
            key,
            age,
        )
    assert await maintenance._prune_lookup_cache(db) == 4
    left = [r["key"] for r in await db.fetch("select key from lookup_cache order by key")]
    assert left == ["bsky:q:fresh", "gtrends:seen", "wiki:fresh", "xtrends:seen"]
    assert await maintenance._prune_lookup_cache(db) == 0


@needs_db
async def test_repair_logo_hashes_fixes_a_missed_copy_inside_the_window(
    db: asyncpg.Connection,
) -> None:
    for mint, age in (("recent", "1 hour"), ("old", "30 days")):
        await db.execute(
            "insert into token (mint, created_at) values ($1, now() - $2::text::interval)",
            mint,
            age,
        )
        await db.execute(
            "insert into token_metadata (mint, status, image_content_key) values ($1, 'ok', 'k')",
            mint,
        )
    await db.execute("insert into image (content_key, phash) values ('k', 42)")
    # the triggers copied the hash; simulate a copy two racing writes left behind
    await db.execute("update token set logo_phash = null")
    assert await maintenance._repair_logo_hashes(db) == 1
    rows = dict(await db.fetch("select mint, logo_phash from token"))  # type: ignore[arg-type]
    assert rows == {"recent": 42, "old": None}  # only the logo scan window is repaired
    assert await maintenance._repair_logo_hashes(db) == 0
