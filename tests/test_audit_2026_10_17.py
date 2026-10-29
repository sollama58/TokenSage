"""TokenSage audit 2026-10-17: stale story heat is not served as fresh (M1), a dead worker's
batch item carries its code (M2), the admin panel keeps its key off disk (M5), the heat cache
key carries the lineage original (L1), and "probably refers to" (L3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import needs_db
from tests.fixtures.chain import SPL_MINT
from tokensage import now, queue
from tokensage.api.schemas import StoryHeat
from tokensage.db import _init_connection
from tokensage.engine import render_summary
from tokensage.sources import geckoterminal

STATIC = Path(__file__).resolve().parents[1] / "tokensage" / "api" / "static"
ORIGINAL = "MooDengLeader1111111111111111111111111pump"
DOC: dict[str, Any] = {
    "lineage": {"kind": "copy", "of_name": "Moo Deng", "of_mint": ORIGINAL},
}


def _key(story_mint: str | None) -> str:
    return f"{now.HEAT_KEY}v3:lineage:moodeng:{story_mint or ''}"


async def _cache_ok_heat(conn: asyncpg.Connection, key: str, age_s: int) -> None:
    heat = StoryHeat(
        story="Moo Deng", story_kind="lineage", status="ok", as_of=datetime.now(UTC), coins=3
    )
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at)
           values ($1, $2, now() - make_interval(secs => $3))
           on conflict (key) do update set value=excluded.value, fetched_at=excluded.fetched_at""",
        key,
        heat.model_dump(mode="json"),
        age_s,
    )


@needs_db
@pytest.mark.parametrize(
    ("age_s", "served"),
    [
        (int((now.HEAT_TTL + timedelta(minutes=2)).total_seconds()), "ok"),
        (int((now.HEAT_STALE_MAX + timedelta(minutes=1)).total_seconds()), "failed"),
    ],
)
async def test_story_heat_falls_back_only_to_recent_heat(
    migrated_db: str,
    clean_tables: None,
    monkeypatch: pytest.MonkeyPatch,
    age_s: int,
    served: str,
) -> None:
    """M1: a failed refresh serves the last good heat only while it is under 2x HEAT_TTL old;
    an older one would go out as current market data."""

    async def failing(http: httpx.AsyncClient, query: str) -> None:
        return None

    monkeypatch.setattr(geckoterminal, "search_pools", failing)
    conn = await asyncpg.connect(migrated_db)
    try:
        await _init_connection(conn)  # jsonb as dicts, as the pool's connections have
        await conn.execute("delete from lookup_cache where key like 'heat:%'")
        await _cache_ok_heat(conn, _key(ORIGINAL), age_s)
        async with httpx.AsyncClient() as http:
            heat = await now.story_heat(conn, http, DOC)
        assert heat is not None and heat.status == served
        if served == "failed":
            assert heat.leader is None and heat.coins == 0
    finally:
        await conn.close()


@needs_db
async def test_story_heat_key_carries_the_lineage_original(
    migrated_db: str, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: a coin whose original has another mint does not get this story's cached heat."""

    async def failing(http: httpx.AsyncClient, query: str) -> None:
        return None

    monkeypatch.setattr(geckoterminal, "search_pools", failing)
    conn = await asyncpg.connect(migrated_db)
    try:
        await _init_connection(conn)  # jsonb as dicts, as the pool's connections have
        await conn.execute("delete from lookup_cache where key like 'heat:%'")
        await _cache_ok_heat(conn, _key("SomeOtherPepe9999999999999999999999999pump"), 0)
        async with httpx.AsyncClient() as http:
            heat = await now.story_heat(conn, http, DOC)
        assert heat is not None and heat.status == "failed"
        assert await conn.fetchval("select count(*) from lookup_cache where key=$1", _key(ORIGINAL))
    finally:
        await conn.close()


@needs_db
async def test_batch_item_of_a_dead_worker_carries_its_code(
    client: httpx.AsyncClient, migrated_db: str
) -> None:
    """M2: the batch item's error is "<code>: <detail>", so a client sees worker_died as a
    code instead of a free-text reason."""
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            """insert into job (kind, mint, depth, status, attempts, last_error, error_code,
                                finished_at)
               values ('analyze', $1, 'full', 'failed', 3, $2, $3, now())""",
            SPL_MINT,
            queue.WORKER_DIED,
            queue.WORKER_DIED_CODE,
        )
    finally:
        await conn.close()
    r = await client.post("/v1/tokens:batch", json={"cas": [SPL_MINT], "depth": "full"})
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["status"] == "failed"
    assert item["error"].startswith("worker_died: worker died; retried automatically after")


def test_admin_panel_keeps_the_key_off_disk() -> None:
    """M5: the panel's key lives in sessionStorage only, and an old remembered copy goes."""
    src = (STATIC / "admin.html").read_text()
    assert "localStorage.setItem(LS_KEY" not in src
    assert "localStorage.getItem(LS_KEY" not in src
    assert "localStorage.removeItem(LS_KEY)" in src
    assert 'id="remember"' not in src


def test_mid_confidence_referent_wording_is_probable() -> None:
    """L3: the Probable wording convention, not "most likely"."""
    assert render_summary._verb(0.7) == "probably refers to"
    assert render_summary._verb(0.9) == "refers to" and render_summary._verb(0.5) == "may refer to"


def test_geckoterminal_cooldown_doubles_while_refused_and_resets_on_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check-in 2026-10-29: a flat 60 s pause still met 80-90% 429s, so each 429 in a row doubles
    the pause up to COOLDOWN_MAX_S."""
    from tokensage.sources import geckoterminal

    monkeypatch.setattr(geckoterminal.time, "monotonic", lambda: 1000.0)
    waits = []
    for _ in range(6):
        geckoterminal._start_cooldown(None)
        waits.append(geckoterminal._cooldown_until - 1000.0)
    assert waits == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    # a short Retry-After never undercuts the doubling
    geckoterminal._refusals = 1
    geckoterminal._start_cooldown("5")
    assert geckoterminal._cooldown_until - 1000.0 == 120.0
