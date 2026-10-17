"""The hourly maintenance job: the analysis prune keeps the newest 3 versions per mint and
depth without sweeping the whole table, and one failing step does not stop the others."""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------- the token family


async def _token(
    conn: asyncpg.Connection,
    mint: str,
    created: str | None,
    *,
    first_seen: str = "1 hour",
    read: str | None = None,
) -> None:
    await conn.execute(
        """insert into token (mint, name, created_at, first_seen_at)
           values ($1, $1, case when $2::text is null then null
                                else now() - $2::text::interval end,
                   now() - $3::text::interval)""",
        mint,
        created,
        first_seen,
    )
    await conn.execute(
        """insert into token_metadata (mint, status, image_content_key)
           values ($1, 'ok', 'logo-' || $1)""",
        mint,
    )
    await conn.execute(
        """insert into image (content_key, analyzed_at)
           values ('logo-' || $1, now() - interval '1 year')""",
        mint,
    )
    await conn.execute(
        """insert into token_read (mint, launched_at, referent_key, categories)
           values ($1, now(), 'ref', '{}')""",
        mint,
    )
    await conn.execute("insert into x_ref (mint, kind) values ($1, 'tweet')", mint)
    await conn.execute(
        """insert into analysis (mint, version, depth, doc, categories, flags, created_at)
           values ($1, 1, 'basic', '{}'::jsonb, '{}', '{}', now() - $2::text::interval)""",
        mint,
        read or created or first_seen,  # read when it was launched, unless told otherwise
    )


async def _mints(conn: asyncpg.Connection, table: str) -> set[str]:
    return {r["mint"] for r in await conn.fetch(f"select mint from {table}")}


@needs_db
async def test_prune_tokens_drops_detail_at_30_days_and_the_row_at_90(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.execute("truncate token_metadata, token_read, x_ref, image cascade")
    await db.execute(
        """update maintenance_cursor set at = now() - interval '1000 days'
           where name = 'token_detail'"""
    )
    await _token(db, "ancient", "100 days")
    await _token(db, "aged", "40 days")
    await _token(db, "undated-old", None, first_seen="40 days")
    await _token(db, "undated-new", None, first_seen="10 days")
    await _token(db, "young", "10 days")
    # launched long ago but read yesterday (a pair token, an old coin looked up): kept whole
    await _token(db, "ancient-read", "100 days", read="1 day")
    # aged has a newer full read and a newer basic one: only the superseded basic v1 goes
    for v, depth in [(2, "full"), (3, "basic")]:
        await db.execute(
            """insert into analysis (mint, version, depth, doc, categories, flags, created_at)
               values ('aged', $1, $2, '{}'::jsonb, '{}', '{}', now() - interval '35 days')""",
            v,
            depth,
        )
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 1)  # several batches, one token each

    out = await maintenance._prune_tokens(db)
    # ancient, ancient-read and aged lost token_metadata and token_read (2 rows each), aged
    # also its superseded analysis; each kept its newest analysis per depth. ancient then
    # went whole, as did the undated token seen 40 days ago; ancient-read was read recently
    assert out["tokens"] == 1 and out["undated_tokens"] == 1
    assert out["detail_rows"] == 7
    # the cursor rests on the aged token, ten days behind the 30-day line
    assert out["detail_lag_s"] is not None and out["detail_lag_s"] <= 11 * 86400
    kept = {"aged", "ancient-read", "undated-new", "young"}
    assert await _mints(db, "token") == kept
    assert await _mints(db, "x_ref") == kept  # slim rows stay
    assert await _mints(db, "analysis") == kept
    assert await _versions(db, "aged") == [2, 3]
    assert await _mints(db, "token_metadata") == {"undated-new", "young"}
    assert await _mints(db, "token_read") == {"undated-new", "young"}
    # a second run finds nothing: the cursor has passed the aged token
    again = await maintenance._prune_tokens(db)
    assert again["detail_rows"] == 0 and again["tokens"] == 0 and again["batches"] <= 2

    # a token first read long after its launch arrives behind the cursor: the detail walk
    # does not go back for it, and with the row horizon off (0) it stays whole
    await _token(db, "late-read", "100 days")
    monkeypatch.setenv("TOKEN_RETENTION_DAYS", "0")
    maintenance.get_settings.cache_clear()
    out = await maintenance._prune_tokens(db)
    assert out["tokens"] == 0 and out["detail_rows"] == 0
    assert "late-read" in await _mints(db, "analysis")
    # ...and goes whole once the row horizon is back on
    monkeypatch.delenv("TOKEN_RETENTION_DAYS")
    maintenance.get_settings.cache_clear()
    assert (await maintenance._prune_tokens(db))["tokens"] == 1
    assert "late-read" not in await _mints(db, "token")


@needs_db
async def test_cache_sweep_drops_orphan_images_and_expired_caches_once_a_day(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.execute("truncate token_metadata, image, pair_token, x_profile_history cascade")
    await _token(db, "live", "10 days")  # its logo row is old but still pointed at
    await db.execute(
        """insert into image (content_key, analyzed_at)
           values ('orphan-old', now() - interval '40 days'),
                  ('orphan-new', now() - interval '10 days')"""
    )
    await db.execute(
        """insert into pair_token (mint, source, fetched_at)
           values ('pair-old', 'none', now() - interval '40 days'),
                  ('pair-new', 'none', now() - interval '1 day')"""
    )
    await db.execute(
        """insert into x_profile_history (user_id, handle, followers, seen_at)
           values ('u1', 'h', 1, now() - interval '40 days'),
                  ('u1', 'h', 2, now() - interval '1 day')"""
    )
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 1)
    # outside the sweep hour the step is a no-op...
    monkeypatch.setattr(maintenance, "CACHE_SWEEP_HOUR_UTC", -1)
    assert await maintenance._sweep_caches(db) is None
    # ...and forced (or at the hour) it sweeps every cache on the detail horizon
    out = await maintenance._sweep_caches(db, force=True)
    assert out is not None
    assert out["image"] == 1 and out["pair_token"] == 1 and out["x_profile_history"] == 1
    keys = {r["content_key"] for r in await db.fetch("select content_key from image")}
    assert keys == {"logo-live", "orphan-new"}
    assert await _mints(db, "pair_token") == {"pair-new"}
    assert await db.fetchval("select count(*) from x_profile_history") == 1


@needs_db
async def test_condense_strips_heavy_parts_of_old_reads_once(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.execute(
        """update maintenance_cursor set at = now() - interval '1000 days'
           where name = 'analysis_condense'"""
    )
    full = {
        "mint": "m",
        "summary": "s",
        "raw": {"name": "Dog"},
        "referent": {"label": "dog"},
        "categories": [{"label": "animal", "confidence": 0.9}],
        "lineage": {"original": "o"},
        "evidence": [{"kind": "k"}],
        "market": {"mc": 1},
        "x": {"handle": "h"},
        "image": {"ocr": ["x"]},
        "trend": {"terms": []},
        "caveats": ["partial: x"],
    }
    rows = [
        ("old", 1, "basic", "10 days"),
        ("old", 2, "full", "9 days"),
        ("new", 1, "basic", "1 day"),
    ]
    for mint, v, depth, age in rows:
        await db.execute("insert into token (mint) values ($1) on conflict do nothing", mint)
        await db.execute(
            """insert into analysis (mint, version, depth, doc, categories, flags, created_at)
               values ($1, $2, $3, $4::jsonb, '{}', '{}', now() - $5::text::interval)""",
            mint,
            v,
            depth,
            json.dumps(full),
            age,
        )
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 1)  # one row per batch

    out = await maintenance._condense_analyses(db)
    assert out is not None and out["condensed"] == 2
    assert out["lag_s"] is not None and out["lag_s"] <= 2 * 86400 + 60

    async def doc(mint: str, v: int) -> dict[str, Any]:
        return json.loads(
            await db.fetchval("select doc from analysis where mint=$1 and version=$2", mint, v)
        )

    basic, fullread, young = await doc("old", 1), await doc("old", 2), await doc("new", 1)
    kept = {"mint", "summary", "raw", "referent", "categories", "lineage", "caveats"}
    assert set(basic) == kept
    assert set(fullread) == kept | {"x", "image", "trend"}  # what /v1/tokens:now reads
    assert basic["caveats"] == ["partial: x", maintenance.CONDENSED_CAVEAT]
    assert young == full
    # a rerun finds nothing new, and a cursor put back rewrites nothing twice
    assert (await maintenance._condense_analyses(db))["condensed"] == 0  # type: ignore[index]
    await db.execute(
        """update maintenance_cursor set at = now() - interval '1000 days'
           where name = 'analysis_condense'"""
    )
    assert (await maintenance._condense_analyses(db))["condensed"] == 0  # type: ignore[index]
    assert await doc("old", 1) == basic

    monkeypatch.setenv("ANALYSIS_CONDENSE_DAYS", "0")
    maintenance.get_settings.cache_clear()
    assert await maintenance._condense_analyses(db) is None
