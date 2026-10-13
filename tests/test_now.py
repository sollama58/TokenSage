"""The decision-time refresh (rules 0.34.0): POST /v1/tokens:now, a fresh trend block on the
stored read's identity, and the story's heat from GeckoTerminal."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import SPL_MINT
from tokensage import now
from tokensage.engine import trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.sources import geckoterminal

LEADER = "MooDengLeader1111111111111111111111111pump"
OTHER = "MooDengOther22222222222222222222222222pump"


def _pool(addr: str, mint: str, name: str, sym: str, mcap: float, h1: float, v1: float) -> dict:
    return {
        "id": f"solana_{addr}",
        "type": "pool",
        "attributes": {
            "address": addr,
            "name": f"{sym} / SOL",
            "market_cap_usd": str(mcap) if mcap else None,
            "fdv_usd": str(mcap * 1.1),
            "pool_created_at": "2026-09-10T00:00:00Z",
            "price_change_percentage": {"m5": "1.5", "h1": str(h1)},
            "volume_usd": {"m5": str(v1 / 12), "h1": str(v1)},
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}", "type": "token"}}},
    }


def _token(mint: str, name: str, sym: str) -> dict:
    return {
        "id": f"solana_{mint}",
        "type": "token",
        "attributes": {"address": mint, "name": name, "symbol": sym},
    }


SEARCH = {
    "data": [
        _pool("P1", LEADER, "Moo Deng", "MOODENG", 90_000_000, -4.0, 1_200_000),
        _pool("P2", OTHER, "Baby Moo Deng", "BMD", 40_000, 25.0, 30_000),
        _pool("P3", LEADER, "Moo Deng", "MOODENG", 0, 0.0, 100_000),  # its second pool
        _pool("P4", "Unrelated3333333333333333333333333333pump", "Hippo", "HIPPO", 5e6, 1, 1),
    ],
    "included": [
        _token(LEADER, "Moo Deng", "MOODENG"),
        _token(OTHER, "Baby Moo Deng", "BMD"),
        _token("Unrelated3333333333333333333333333333pump", "Hippo", "HIPPO"),
    ],
}


def _ohlcv(now_s: float) -> dict:
    # peak 3 hours ago at 2.0, now 1.5
    rows = [
        [now_s - 15 * 60 * i, 1.0, 2.0 if i == 12 else 1.6, 0.9, 1.5 if i == 0 else 1.2, 10]
        for i in range(96)
    ]
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def test_parse_pools_and_peak() -> None:
    pools = geckoterminal.parse_pools(SEARCH)
    assert [p.mint for p in pools][:2] == [LEADER, OTHER]
    assert pools[0].mcap_usd == 90_000_000 and pools[0].change_1h_pct == -4.0
    assert pools[2].mcap_usd == pytest.approx(0.0 * 1.1) or pools[2].mcap_usd is None
    peak = geckoterminal.parse_peak(_ohlcv(1_000_000.0), 1_000_000.0)
    assert peak is not None and peak.minutes_ago == 180 and peak.from_peak_pct == -25.0
    assert geckoterminal.parse_peak({"data": {}}, 0) is None


def test_heat_from_pools_keeps_the_story_and_sums_pools() -> None:
    pools = geckoterminal.parse_pools(SEARCH)
    heat, lead = now.heat_from_pools(pools, "Moo Deng", "referent", None)
    assert heat.coins == 2 and lead is not None and heat.leader is not None
    assert heat.leader.mint == LEADER and heat.leader.pool == "P1"
    assert heat.leader.volume_1h_usd == 1_300_000
    assert heat.wave_volume_1h_usd == 1_330_000
    # a lineage original is on its story whatever its name says
    heat, _ = now.heat_from_pools(pools, "Something Else", "lineage", OTHER)
    assert heat.coins == 1 and heat.leader and heat.leader.mint == OTHER


def test_story_of_prefers_the_lineage_original() -> None:
    doc: dict[str, Any] = {
        "referent": {"label": "Moo Deng", "generic": False},
        "lineage": {"kind": "late_copy", "of_name": "Baby Moo Deng", "of_mint": OTHER},
    }
    assert now.story_of(doc) == ("Baby Moo Deng", "lineage", OTHER)
    doc["lineage"]["kind"] = "original"
    assert now.story_of(doc) == ("Moo Deng", "referent", None)
    doc["referent"]["generic"] = True
    assert now.story_of(doc) is None


def test_fresh_trend_matches_the_stored_identity() -> None:
    k = load_knowledge()
    term = trends.TrendTerm(
        "Moo Deng",
        0.0,
        3,
        "x_trends",
        seen_at=datetime.now(UTC) - timedelta(hours=1),
        rank=2,
        days_listed=0,
        direction="rising",
    )
    idx = trends.TrendIndex([term, trends.TrendTerm("time", 0.0, 9000, "google_trends")], k)
    doc = {
        "raw": {"name": "Moo Deng", "symbol": "MOODENG", "description": "it is time"},
        "referent": {"label": "Moo Deng", "generic": False, "source": "entities:Moo Deng"},
    }
    t = now.fresh_trend(doc, idx)
    assert t.matched and [x.term for x in t.terms] == ["Moo Deng"]
    assert t.novelty is not None and t.novelty > 0.9


def _mock_upstreams(router: respx.MockRouter) -> None:
    router.get(url__startswith="https://api.geckoterminal.com/api/v2/search/pools").mock(
        return_value=httpx.Response(200, json=SEARCH)
    )
    router.get(url__regex=r"https://api\.geckoterminal\.com/.*/ohlcv/").mock(
        return_value=httpx.Response(200, json=_ohlcv(time.time()))
    )
    router.get(url__startswith="https://trends24.in/").mock(return_value=httpx.Response(503))
    router.get(url__startswith="https://trends.google.com/").mock(return_value=httpx.Response(503))


@needs_db
async def test_tokens_now_end_to_end(client: httpx.AsyncClient, migrated_db: str) -> None:
    from tokensage import fulldepth

    fulldepth._trend_cache = None
    geckoterminal._calls.clear()
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("delete from lookup_cache where key like 'now:%' or key like 'heat:%'")
        await conn.execute(
            "insert into token (mint, created_at) values ($1, now()) on conflict do nothing",
            SPL_MINT,
        )
        await conn.execute(
            "insert into analysis (mint, version, depth, doc) values ($1, 1, 'full', $2::jsonb)",
            SPL_MINT,
            json.dumps(
                {
                    "raw": {"name": "Moo Deng Jr", "symbol": "MDJ"},
                    "referent": {"label": "Moo Deng", "generic": False, "source": "entities:Moo"},
                    "lineage": {"kind": "original"},
                }
            ),
        )
    finally:
        await conn.close()
    with respx.mock(assert_all_called=False) as router:
        _mock_upstreams(router)
        r = await client.post("/v1/tokens:now", json={"cas": [SPL_MINT, "garbage"], "wait": 8})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[1]["status"] == "invalid"
    it = items[0]
    assert it["status"] == "complete", it
    assert it["trend"] is not None and it["trend"]["sources"]
    heat = it["story_heat"]
    assert heat["status"] == "ok" and heat["story"] == "Moo Deng" and heat["coins"] == 2
    assert heat["leader"]["mint"] == LEADER and heat["leader"]["peak_minutes_ago"] in (179, 180)
    # served from the stored refresh: no upstream call
    with respx.mock(assert_all_called=False) as router:
        r = await client.post("/v1/tokens:now", json={"cas": [SPL_MINT]})
        assert not router.calls
    assert r.json()["items"][0]["status"] == "complete"


@needs_db
async def test_tokens_now_without_a_full_read(client: httpx.AsyncClient, migrated_db: str) -> None:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("delete from lookup_cache where key like 'now:%'")
    finally:
        await conn.close()
    r = await client.post("/v1/tokens:now", json={"cas": [SPL_MINT]})
    assert r.status_code == 200
    assert r.json()["items"][0] == {
        "ca": SPL_MINT,
        "status": "unavailable",
        "as_of": None,
        "trend": None,
        "story_heat": None,
        "error": "no_full_read",
    }
