"""Phase 5: per-key quotas, usage counters, queue back-pressure, signed callbacks."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import ADMIN_KEY, API_KEY, needs_db
from tests.fixtures.chain import CID_META, FakeChain, install_web, public_resolver
from tokensage import callbacks
from tokensage.api.auth import sha256_hex
from tokensage.config import Settings
from tokensage.net import safe_fetch

pytestmark = needs_db
MINT = "PuuQ746gUr4mUkMzZhaYkgQomxQNpVgQfdEmZUVpump"
HOOK = "https://hook.test/tokensage"


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[respx.MockRouter]:  # type: ignore[misc]
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    with respx.mock(assert_all_called=False) as r:
        yield r


def _install_chain(router: respx.MockRouter) -> None:
    chain = FakeChain()
    chain.add_t22_pump(MINT, "Fear Of Missing Out", "FOMO", f"https://ipfs.io/ipfs/{CID_META}")
    install_web(router, chain)


@contextlib.asynccontextmanager
async def make_client(db: str, **over: Any) -> AsyncIterator[httpx.AsyncClient]:
    from tokensage.api.app import create_app

    base: dict[str, Any] = dict(
        database_url=db,
        api_keys=f"tester:{API_KEY}",
        admin_key=ADMIN_KEY,
        inline_analyzer=True,
        default_wait_s=5,
        solana_rpc_url="https://rpc.test/",
        ipfs_gateways="https://gw1.test,https://gw2.test",
        worker_poll_interval_s=0.2,
    )
    base.update(over)
    app = create_app(Settings(**base, _env_file=None))  # type: ignore[call-arg]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as c,
    ):
        yield c


async def test_usage_counters_and_readyz(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    _install_chain(router)
    async with make_client(migrated_db) as c:
        r = await c.get(f"/v1/tokens/{MINT}", params={"depth": "basic", "wait": 5})
        assert r.status_code == 200, r.text
        r = await c.get(f"/v1/tokens/{MINT}", params={"depth": "basic"})
        assert r.status_code == 200
        ready = await c.get("/readyz", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert ready.status_code == 200
        assert ready.headers["cache-control"] == "no-store"
        usage = {u["key_name"]: u for u in ready.json()["usage_today"]}
        assert usage["tester"]["requests"] == 2
        assert usage["tester"]["full_calls"] == 0
    conn = await asyncpg.connect(migrated_db)
    try:
        row = await conn.fetchrow("select requests, full_calls, refreshes from api_usage")
        assert dict(row) == {"requests": 2, "full_calls": 0, "refreshes": 0}
    finally:
        await conn.close()


async def test_full_quota_exhausted_returns_429(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    _install_chain(router)
    async with make_client(migrated_db, full_per_day_default=0) as c:
        r = await c.get(f"/v1/tokens/{MINT}", params={"depth": "full"})
        assert r.status_code == 429, r.text
        body = r.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert 0 < int(r.headers["retry-after"]) <= 86400
        # basic depth is unaffected by the full quota
        r = await c.get(f"/v1/tokens/{MINT}", params={"depth": "basic", "wait": 5})
        assert r.status_code == 200, r.text


async def test_refresh_quota_exhausted_returns_429(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    _install_chain(router)
    async with make_client(migrated_db, refresh_per_day_default=1) as c:
        r = await c.get(f"/v1/tokens/{MINT}", params={"depth": "basic", "wait": 5})
        assert r.status_code == 200, r.text
        r = await c.get(f"/v1/tokens/{MINT}", params={"refresh": "true", "wait": 5})
        assert r.status_code == 200, r.text
        r = await c.get(f"/v1/tokens/{MINT}", params={"refresh": "true", "wait": 5})
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "quota_exceeded"
        # a plain cached read still works once the refresh quota is gone
        r = await c.get(f"/v1/tokens/{MINT}")
        assert r.status_code == 200
        assert r.json()["freshness"]["from_cache"] is True


async def test_queue_full_returns_503_overloaded(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    _install_chain(router)
    async with make_client(migrated_db, max_queue_depth=0, inline_analyzer=False) as c:
        r = await c.get(f"/v1/tokens/{MINT}", params={"wait": 0})
        assert r.status_code == 503, r.text
        assert r.json()["error"]["code"] == "overloaded"
        assert r.headers["retry-after"]
        # batch items are rejected one by one instead of failing the whole request
        b = await c.post("/v1/tokens:batch", json={"cas": [MINT]})
        assert b.status_code == 200
        item = b.json()["items"][0]
        assert item["status"] == "failed" and item["error"] == "overloaded"
        assert item["job_id"] is None and item["retry_after_s"]


async def test_invalid_callback_url_is_rejected(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    async with make_client(migrated_db, inline_analyzer=False) as c:
        for bad in ("http://hook.test/x", "https://127.0.0.1/x", "https://localhost/x"):
            r = await c.post("/v1/tokens:batch", json={"cas": [MINT], "callback_url": bad})
            assert r.status_code == 400, (bad, r.text)
            assert r.json()["error"]["code"] == "invalid_callback_url"
    conn = await asyncpg.connect(migrated_db)
    try:
        assert await conn.fetchval("select count(*) from job") == 0
    finally:
        await conn.close()


async def test_batch_callback_is_delivered_and_signed(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    received: list[httpx.Request] = []

    def hook(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(204)

    router.post(HOOK).mock(side_effect=hook)
    _install_chain(router)
    async with make_client(migrated_db) as c:
        r = await c.post("/v1/tokens:batch", json={"cas": [MINT], "callback_url": HOOK})
        assert r.status_code == 200, r.text
        item = r.json()["items"][0]
        assert item["status"] == "pending" and item["job_id"]
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.1)
    assert received, "callback never delivered"
    req = received[0]
    secret = sha256_hex(API_KEY)
    ts = int(req.headers["X-TokenSage-Timestamp"])
    assert req.headers["X-TokenSage-Job"] == str(item["job_id"])
    assert callbacks.verify(secret, ts, req.content, req.headers["X-TokenSage-Signature"])
    assert not callbacks.verify("00" * 32, ts, req.content, req.headers["X-TokenSage-Signature"])
    body = httpx.Response(200, content=req.content).json()
    assert body["job_id"] == item["job_id"]
    assert body["status"] == "done"
    assert body["result"]["analysis"]["mint"] == MINT
    assert body["result"]["ca"] == MINT
    conn = await asyncpg.connect(migrated_db)
    try:
        rows = await conn.fetch("select kind, status from job order by id")
        assert [(r["kind"], r["status"]) for r in rows] == [
            ("analyze", "done"),
            ("callback", "done"),
        ]
    finally:
        await conn.close()


async def test_failed_callback_is_retried_then_given_up(
    migrated_db: str, clean_tables: None, router: respx.MockRouter
) -> None:
    router.post(HOOK).mock(return_value=httpx.Response(500))
    _install_chain(router)
    async with make_client(migrated_db) as c:
        r = await c.post("/v1/tokens:batch", json={"cas": [MINT], "callback_url": HOOK})
        assert r.status_code == 200, r.text
        conn = await asyncpg.connect(migrated_db)
        try:
            for _ in range(100):
                row = await conn.fetchrow(
                    "select status, attempts, last_error from job where kind='callback'"
                )
                if row and row["attempts"] >= 1 and row["status"] != "running":
                    break
                await asyncio.sleep(0.1)
            assert row is not None
            assert row["attempts"] >= 1
            assert "http 500" in (row["last_error"] or "")
            assert row["status"] in ("pending", "failed")
        finally:
            await conn.close()
