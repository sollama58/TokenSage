"""Fixes requested by the main consumer: quota charging, real job/webhook status, per-item
batch rejections, include=raw, quota headers."""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import API_KEY, needs_db
from tests.fixtures.chain import (
    CID_META,
    SPL_MINT,
    T22_MINT,
    FakeChain,
    install_web,
)
from tests.test_phase5_integration import HOOK, make_client, router  # noqa: F401
from tokensage import callbacks
from tokensage.api.auth import sha256_hex

pytestmark = needs_db
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.4)
    c.add_spl_pump(SPL_MINT, "StreamerCoin", "STREAMER", META_URI, complete=True)
    return c


async def _usage(db: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(db)
    try:
        return await conn.fetchrow("select requests, full_calls, refreshes from api_usage")
    finally:
        await conn.close()


async def test_polling_a_pending_full_analysis_charges_one_unit(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    # no analyzer running: the job stays pending, like a slow cold analysis
    async with make_client(migrated_db, inline_analyzer=False) as c:
        heads = []
        for _ in range(3):
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 0})
            assert r.status_code == 202, r.text
            heads.append(r.headers["x-quota-full-remaining"])
        b = await c.post("/v1/tokens:batch", json={"cas": [T22_MINT], "depth": "full"})
        assert b.status_code == 200 and b.json()["items"][0]["status"] == "pending"
    u = await _usage(migrated_db)
    assert u is not None and u["full_calls"] == 1 and u["requests"] == 4
    assert heads == ["1999", "1999", "1999"]
    assert b.headers["x-quota-full-remaining"] == "1999"


async def test_exhausted_quota_still_lets_you_poll_an_open_job(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db, inline_analyzer=False, full_per_day_default=1) as c:
        r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 0})
        assert r.status_code == 202 and r.headers["x-quota-full-remaining"] == "0"
        # polling the same open job is free, even with the quota used up
        r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 0})
        assert r.status_code == 202
        # a different coin needs a new job and is refused, without leaving a job behind
        r = await c.get(f"/v1/tokens/{SPL_MINT}", params={"depth": "full", "wait": 0})
        assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
        assert r.headers["x-quota-full-remaining"] == "0"
    conn = await asyncpg.connect(migrated_db)
    try:
        assert await conn.fetchval("select count(*) from job where mint=$1", SPL_MINT) == 0
    finally:
        await conn.close()


@pytest.mark.parametrize("limit", ["quota", "queue"])
async def test_batch_rejects_items_individually(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
    limit: str,
) -> None:
    install_web(router, _chain())
    over = {"full_per_day_default": 1} if limit == "quota" else {"max_queue_depth": 1}
    async with make_client(migrated_db, inline_analyzer=False, **over) as c:
        r = await c.post("/v1/tokens:batch", json={"cas": [T22_MINT, SPL_MINT], "depth": "full"})
    assert r.status_code == 200, r.text
    first, second = r.json()["items"]
    assert first["status"] == "pending" and first["job_id"]
    assert second["status"] == "failed" and second["job_id"] is None
    assert second["error"] == ("quota_exceeded" if limit == "quota" else "overloaded")
    assert second["retry_after_s"] and second["retry_after_s"] > 0


async def test_jobs_and_webhooks_report_partial(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    received: list[httpx.Request] = []
    router.post(HOOK).mock(side_effect=lambda req: (received.append(req), httpx.Response(204))[1])
    install_web(router, _chain(), gateways_ok=False)  # metadata outage -> partial
    async with make_client(migrated_db, rate_per_min_default=10_000) as c:
        r = await c.post(
            "/v1/tokens:batch", json={"cas": [T22_MINT], "depth": "basic", "callback_url": HOOK}
        )
        job_id = r.json()["items"][0]["job_id"]
        for _ in range(100):
            j = (await c.get(f"/v1/jobs/{job_id}")).json()
            if j["status"] == "done" and received:
                break
            await asyncio.sleep(0.1)
    assert j["status"] == "done" and j["result"]["status"] == "partial"
    assert received, "callback not delivered"
    body = httpx.Response(200, content=received[0].content).json()
    assert body["result"]["status"] == "partial"
    ts = int(received[0].headers["X-TokenSage-Timestamp"])
    assert callbacks.verify(
        sha256_hex(API_KEY), ts, received[0].content, received[0].headers["X-TokenSage-Signature"]
    )


async def test_include_drops_raw_and_evidence(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        full = (await c.get(f"/v1/tokens/{T22_MINT}", params={"wait": 5, "depth": "basic"})).json()
        assert full["analysis"]["raw"]["name"] and full["analysis"]["evidence"]
        ev = (await c.get(f"/v1/tokens/{T22_MINT}", params={"include": "evidence"})).json()
        assert ev["analysis"]["evidence"] and ev["analysis"]["raw"]["name"] is None
        raw = (await c.get(f"/v1/tokens/{T22_MINT}", params={"include": "raw"})).json()
        assert raw["analysis"]["evidence"] == [] and raw["analysis"]["raw"]["name"]
        both = (await c.get(f"/v1/tokens/{T22_MINT}", params={"include": "raw, evidence"})).json()
        assert both["analysis"]["evidence"] and both["analysis"]["raw"]["name"]


async def test_failed_analysis_is_reported_not_requeued(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """After a job fails, asking again (single or batch) reports the failure for a while
    instead of quietly starting a new job and spending quota; refresh=true retries."""
    install_web(router, _chain())
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            """insert into job (kind, mint, depth, status, attempts, last_error, finished_at)
               values ('analyze', $1, 'full', 'failed', 3, 'RpcError: http 503', now())""",
            T22_MINT,
        )
        async with make_client(migrated_db) as c:
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 2})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "failed" and body["job_id"] is not None
            assert "RpcError" in body["errors"][0]["detail"]
            b = await c.post("/v1/tokens:batch", json={"cas": [T22_MINT], "depth": "full"})
            assert b.status_code == 200, b.text
            assert b.json()["items"][0]["status"] == "failed"
            assert await conn.fetchval("select count(*) from job") == 1  # nothing new
            assert await conn.fetchval("select coalesce(sum(full_calls), 0) from api_usage") == 0
            r2 = await c.get(
                f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 10, "refresh": "true"}
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["status"] == "complete"
        assert await conn.fetchval("select count(*) from job") >= 2
    finally:
        await conn.close()
