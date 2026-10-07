"""Upstream metering (Helius credits included) and the admin panel's monitoring reads."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest

from tests.conftest import ADMIN_KEY
from tokensage.api.routes_monitor import billing_cycle
from tokensage.net import metrics
from tokensage.net.breaker import breaker
from tokensage.resolve.rpc import RpcError, SolanaRpc

ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=x"


@pytest.fixture(autouse=True)
def _fresh_meter() -> None:
    metrics.meter.rows.clear()
    metrics.meter._sec.clear()
    metrics.meter.configure(
        rpc_url=RPC_URL, ipfs_gateways=["https://gw1.test", "https://gw2.test"], costs=""
    )


def _rpc_handler(request: httpx.Request) -> httpx.Response:
    import json

    body = json.loads(request.content)
    if body["method"] == "getTransaction":
        return httpx.Response(429)
    if body["method"] == "getSignaturesForAddress":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1}})
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": None}})


def _rows() -> dict[tuple[str, str], metrics.Counter]:
    return {(s, m): c for (_, s, m), c in metrics.meter.rows.items()}


def test_parse_costs_and_classify() -> None:
    assert metrics.parse_costs("getAsset:5, bad, getX:-1,getY:z,getTransaction:10") == {
        "getAsset": 5,
        "getTransaction": 10,
    }
    assert metrics.classify("en.wikipedia.org") == "wikipedia"
    assert metrics.classify("news.google.com") == "google_news"
    assert metrics.classify("frontend-api-v3.pump.fun") == "pump_fun"
    assert metrics.classify("gw2.test") == "ipfs"
    assert metrics.classify("attacker.example") == "other"
    assert metrics.classify("notwikipedia.org") == "other"
    assert metrics.provider_of(RPC_URL) == "helius"


def test_billing_cycle() -> None:
    s, e = billing_cycle(datetime(2026, 10, 7, 12, tzinfo=UTC), 1)
    assert (s.month, s.day, e.month, e.day) == (10, 1, 11, 1)
    s, e = billing_cycle(datetime(2026, 10, 7, tzinfo=UTC), 15)
    assert (s.month, s.day, e.month, e.day) == (9, 15, 10, 15)
    s, e = billing_cycle(datetime(2026, 1, 3, tzinfo=UTC), 20)
    assert (s.year, s.month, e.year, e.month) == (2025, 12, 2026, 1)
    s, e = billing_cycle(datetime(2026, 12, 25, tzinfo=UTC), 20)
    assert (s.month, e.year, e.month) == (12, 2027, 1)


@pytest.mark.asyncio
async def test_rpc_calls_are_metered_with_credits() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_rpc_handler)) as http:
        rpc = SolanaRpc(RPC_URL, http)
        await rpc.get_account_info("A")
        await rpc.get_multiple_accounts(["A", "B"])
        await rpc.get_asset("A")
        with pytest.raises(RpcError):
            await rpc.get_transaction("sig")  # 429: counted, not billed
        with pytest.raises(RpcError):
            await rpc.get_signatures("A")  # JSON-RPC error: billed
    rows = _rows()
    assert rows[("solana_rpc", "getAccountInfo")].credits == 1
    assert rows[("solana_rpc", "getMultipleAccounts")].credits == 1
    assert rows[("solana_rpc", "getAsset")].credits == 10
    tx = rows[("solana_rpc", "getTransaction")]
    assert (tx.calls, tx.credits, tx.errors, tx.rate_limited) == (1, 0, 1, 1)
    sigs = rows[("solana_rpc", "getSignaturesForAddress")]
    assert (sigs.calls, sigs.credits, sigs.errors) == (1, 1, 1)
    assert max(c.peak_rps for c in rows.values()) >= 2


@pytest.mark.asyncio
async def test_transport_labels_upstreams_and_skips_the_rpc_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "news.google.com":
            return httpx.Response(503)
        return httpx.Response(200, json={})

    transport = metrics.MeteredTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport) as http:
        await http.get("https://en.wikipedia.org/w/api.php")
        await http.get("https://news.google.com/rss")
        await http.get("https://gw1.test/ipfs/x")
        await http.get("https://somewhere.example/meta.json")
        await http.post(RPC_URL, json={})  # SolanaRpc records these itself
    rows = _rows()
    assert set(rows) == {
        ("wikipedia", "GET"),
        ("google_news", "GET"),
        ("ipfs", "GET"),
        ("other", "GET"),
    }
    assert rows[("google_news", "GET")].errors == 1


@pytest.mark.asyncio
async def test_flush_adds_across_flushes_and_keeps_counts_on_failure(migrated_db: str) -> None:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate upstream_usage, source_health")
        now = time.time()
        for _ in range(3):
            metrics.meter.record("solana_rpc", "getAccountInfo", ok=True, ms=10, credits=1, now=now)
        await metrics.flush(conn)
        metrics.meter.record("solana_rpc", "getAccountInfo", ok=False, ms=50, credits=1, now=now)
        await metrics.flush(conn)
        row = await conn.fetchrow("select * from upstream_usage")
        assert (row["calls"], row["errors"], row["credits"], row["ms_max"]) == (4, 1, 4, 50)

        metrics.meter.record("wikipedia", "GET", ok=True, ms=1)
        bad = await asyncpg.connect(migrated_db)
        await bad.close()
        with pytest.raises(asyncpg.InterfaceError):
            await metrics.flush(bad)
        assert _rows()[("wikipedia", "GET")].calls == 1  # put back for the next flush
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_breaker_rows_age_out_once_recovered(migrated_db: str) -> None:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate source_health")
        for _ in range(5):
            breaker.failure("wikipedia")
        await breaker.persist(conn)
        assert await conn.fetchval("select state from source_health") == "open"
        breaker.success("wikipedia")
        await breaker.persist(conn)
        assert await conn.fetchval("select count(*) from source_health") == 1  # not stale yet
        await conn.execute("update source_health set updated_at = now() - interval '1 hour'")
        await breaker.persist(conn)
        assert await conn.fetchval("select count(*) from source_health") == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_monitor_routes(client: httpx.AsyncClient, migrated_db: str) -> None:
    for path in ("/admin/v1/helius", "/admin/v1/upstreams", "/admin/v1/queue", "/admin/v1/signals"):
        assert (await client.get(path)).status_code == 403, path

    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate upstream_usage, source_health")
        now = time.time()
        for _ in range(4):
            metrics.meter.record("solana_rpc", "getAccountInfo", ok=True, ms=20, credits=1, now=now)
        metrics.meter.record("solana_rpc", "getAsset", ok=True, ms=80, credits=10, now=now)
        metrics.meter.record(
            "solana_rpc", "getAccountInfo", ok=True, ms=20, credits=1, now=now - 2 * 86400
        )
        metrics.meter.record("google_news", "GET", ok=False, ms=300, rate_limited=True, now=now)
        await metrics.flush(conn)
        await conn.execute(
            """insert into job (kind, mint, depth, status, created_at, started_at, finished_at)
               values ('analyze', 'M1', 'basic', 'done', now() - interval '10 s',
                       now() - interval '8 s', now()),
                      ('analyze', 'M2', 'full', 'failed', now() - interval '5 s',
                       now() - interval '4 s', now())"""
        )
        await conn.execute(
            "update job set error_code='not_found', last_error='gone' where mint='M2'"
        )
    finally:
        await conn.close()

    h = (await client.get("/admin/v1/helius", headers=ADMIN)).json()
    assert h["rpc_configured"] is True
    assert h["credits"]["today"] == 14
    assert h["credits"]["this_hour"] == 14
    assert h["credits"]["last_7d"] == 15
    assert h["plan"]["credits_per_cycle"] == 1_000_000
    assert h["credits"]["projected_cycle"] >= h["credits"]["cycle_to_date"]
    by_method = {m["method"]: m for m in h["methods"]}
    assert by_method["getAsset"]["credit_cost"] == 10
    assert by_method["getAccountInfo"]["calls_today"] == 4
    assert len(h["hourly"]) == 48 and h["hourly"][-1]["credits"] == 14
    assert len(h["daily"]) == 30 and h["daily"][-1]["credits"] == 14

    u = (await client.get("/admin/v1/upstreams", headers=ADMIN)).json()
    src = {s["source"]: s for s in u["sources"]}
    assert src["solana_rpc"]["calls"] == 5
    assert src["google_news"]["error_rate"] == 1.0
    assert src["google_news"]["rate_limited"] == 1

    q = (await client.get("/admin/v1/queue", headers=ADMIN)).json()
    depth = {d["depth"]: d for d in q["by_depth"]}
    assert depth["basic"]["done"] == 1
    assert 1.5 < depth["basic"]["wait_p50_s"] < 2.5
    assert depth["full"]["failed"] == 1
    assert q["errors"][0]["error_code"] == "not_found"
    assert len(q["hourly"]) == 24

    s = (await client.get("/admin/v1/signals", headers=ADMIN)).json()
    assert {k["name"] for k in s["knowledge"]} >= {"trend_term", "known_coin", "top_volume"}
    assert s["paid_x"]["enabled"] is False


@pytest.mark.asyncio
async def test_admin_page_is_served_without_data(client: httpx.AsyncClient) -> None:
    r = await client.get("/admin", headers={})
    assert r.status_code == 200
    assert "TokenSage Admin" in r.text and "/admin/v1/helius" in r.text
    assert r.headers["x-frame-options"] == "DENY"
