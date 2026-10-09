"""Audit 0.28, runtime group: job leases and attempt ceilings (CONC-1, RT-4, RT-7), logo and
metadata retries (RT-1, RT-2), the hints/404 cooldown (RT-3), API contract fixes (API-1,
API-3, API-4/SEC-5, API-5, API-7), the static pages (API-8, API-9), the CI DB guard (TDD-3)
and the wiki text cap (SEC-1)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import ADMIN_KEY, needs_db
from tests.fixtures.chain import (
    CID_IMG,
    CID_META,
    GW1,
    GW2,
    MISSING,
    PNG,
    RPC,
    T22_MINT,
    FakeChain,
    install_web,
    metadata_json,
    public_resolver,
)
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage import analyzer, queue
from tokensage.config import Settings
from tokensage.db import _init_connection, create_pool
from tokensage.net import safe_fetch
from tokensage.worker import Worker

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "tokensage" / "api" / "static"
META_URI = f"https://ipfs.io/ipfs/{CID_META}"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
HINTS = {"name": "Peanut the Squirrel 2.0", "symbol": "PNUT2", "description": "from the caller"}


@pytest.fixture
async def conn(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_db)
    await _init_connection(c)
    try:
        await c.execute("truncate token_metadata, image cascade")
        yield c
    finally:
        await c.close()


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.4)
    return c


# ----------------------------------------------------------------- queue: leases


@needs_db
async def test_stale_lease_holder_cannot_touch_a_reclaimed_job(conn: asyncpg.Connection) -> None:
    """CONC-1: after A's lease expired and B re-claimed the job, A's complete/fail/renew are
    no-ops (B owns the row); B's complete lands."""
    j = await queue.enqueue(conn, "analyze", T22_MINT, "full")
    a = await queue.claim(conn, lease_s=60)
    assert a and a.id == j.id and a.lease_token
    await conn.execute(
        "update job set locked_until = now() - interval '1 second' where id=$1", j.id
    )
    b = await queue.claim(conn, lease_s=60)
    assert b and b.id == j.id and b.lease_token and b.lease_token != a.lease_token

    assert await queue.renew_lease(conn, j.id, 60, a.lease_token) is False
    assert await queue.complete(conn, j.id, 7, a.lease_token) is False
    row = await conn.fetchrow("select status, result_version, attempts from job where id=$1", j.id)
    assert row and row["status"] == "running" and row["result_version"] is None
    assert (
        await queue.fail(conn, j.id, "A: transient", max_attempts=3, lease_token=a.lease_token)
        is None
    )
    assert (await conn.fetchval("select status from job where id=$1", j.id)) == "running"
    assert await queue.release(conn, j.id, a.lease_token) is False
    assert await queue.defer(conn, j.id, 5, a.lease_token) is False

    assert await queue.renew_lease(conn, j.id, 60, b.lease_token) is True
    assert await queue.complete(conn, j.id, 8, b.lease_token) is True
    row = await conn.fetchrow(
        "select status, result_version, lease_token from job where id=$1", j.id
    )
    assert row and row["status"] == "done" and row["result_version"] == 8
    assert row["lease_token"] is None


@needs_db
async def test_claim_fails_a_job_whose_worker_died_too_often(
    conn: asyncpg.Connection, migrated_db: str
) -> None:
    """RT-4: an expired lease is re-claimable only while attempts are left; past the ceiling
    the job is failed ("worker died") and its waiters/callbacks released."""
    from tokensage import callbacks

    j = await queue.enqueue(conn, "analyze", T22_MINT, "full")
    pool = await create_pool(migrated_db, min_size=1, max_size=2)
    waiter = queue.DoneWaiter(pool)
    await waiter.start()
    try:
        for n in (1, 2):
            c = await queue.claim(conn, lease_s=60, kinds=["analyze"], max_attempts=2)
            assert c and c.id == j.id and c.attempts == n
            await conn.execute(
                "update job set locked_until = now() - interval '1 second' where id=$1", j.id
            )
        await callbacks.schedule(
            conn, target_job_id=j.id, callback_url="https://hook.test/x", key_digest="00"
        )
        waiting = asyncio.create_task(waiter.wait(j.id, timeout_s=5))
        await asyncio.sleep(0.1)
        assert await queue.claim(conn, lease_s=60, kinds=["analyze"], max_attempts=2) is None
        row = await conn.fetchrow(
            "select status, attempts, last_error, finished_at from job where id=$1", j.id
        )
        assert row and row["status"] == "failed" and row["attempts"] == 2
        assert row["last_error"] == queue.WORKER_DIED and row["finished_at"] is not None
        assert await waiting is True  # CHANNEL_DONE was notified
        cb = await queue.claim(conn, lease_s=60, max_attempts=3)
        assert cb is not None and cb.kind == "callback"  # the callback was released
    finally:
        await waiter.stop()
        await pool.close()


@needs_db
async def test_a_pending_job_at_the_ceiling_is_still_claimed(conn: asyncpg.Connection) -> None:
    """Review of RT-4: the attempts ceiling applies to expired leases only. A pending job
    whose own ceiling is higher (callbacks retry 3 times) is not stranded by a lower
    JOB_MAX_ATTEMPTS; fail() alone decides a pending job's retry budget."""
    j = await queue.enqueue(conn, "analyze", T22_MINT, "basic")
    await conn.execute("update job set attempts = 2 where id=$1", j.id)
    c = await queue.claim(conn, lease_s=60, kinds=["analyze"], max_attempts=2)
    assert c is not None and c.id == j.id and c.attempts == 3
    row = await conn.fetchrow("select status from job where id=$1", j.id)
    assert row and row["status"] == "running"


@needs_db
async def test_requeue_expired_respects_the_attempt_ceiling(conn: asyncpg.Connection) -> None:
    """RT-4: maintenance returns an expired lease to pending only while attempts are left."""
    fresh = await queue.enqueue(conn, "analyze", T22_MINT, "basic")
    spent = await queue.enqueue(conn, "analyze", T22_MINT, "full")
    for _ in range(2):
        assert await queue.claim(conn, lease_s=60, kinds=["analyze"])
    await conn.execute("update job set locked_until = now() - interval '1 second'")
    await conn.execute("update job set attempts = 3 where id=$1", spent.id)
    assert await queue.requeue_expired(conn, max_attempts=3) == 1
    rows = {r["id"]: r for r in await conn.fetch("select id, status, last_error from job")}
    assert rows[fresh.id]["status"] == "pending"
    assert (
        rows[spent.id]["status"] == "failed" and rows[spent.id]["last_error"] == queue.WORKER_DIED
    )


@needs_db
async def test_interactive_join_unparks_a_job_waiting_for_its_retry(
    conn: asyncpg.Connection,
) -> None:
    """RT-7: a job parked by fail() for its retry delay becomes runnable now when a
    higher-priority (API) request joins it; a same-priority joiner leaves the park."""
    j = await queue.enqueue(conn, "analyze", T22_MINT, "full", priority=queue.PRIORITY_BATCH)
    assert await queue.claim(conn, lease_s=60)
    await queue.fail(conn, j.id, "transient", max_attempts=3, retry_in_s=30)
    assert await queue.claim(conn, lease_s=60) is None  # parked
    again = await queue.enqueue(conn, "analyze", T22_MINT, "full", priority=queue.PRIORITY_BATCH)
    assert again.id == j.id and await queue.claim(conn, lease_s=60) is None  # still parked
    joined = await queue.enqueue(conn, "analyze", T22_MINT, "full", priority=queue.PRIORITY_API)
    assert joined.id == j.id
    claimed = await queue.claim(conn, lease_s=60)
    assert claimed and claimed.id == j.id and claimed.attempts == 2


@needs_db
async def test_worker_stops_a_job_whose_lease_it_lost(
    migrated_db: str, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONC-1: when the heartbeat finds the lease gone, the running analysis is cancelled
    instead of finishing a second copy and overwriting the live run's status."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_analyze(*_a: Any, **_k: Any) -> int:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 1

    async def lost_lease(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr("tokensage.worker.analyze", slow_analyze)
    monkeypatch.setattr(queue, "renew_lease", lost_lease)
    pool = await create_pool(migrated_db, min_size=1, max_size=3)
    settings = Settings(
        database_url=migrated_db,
        solana_rpc_url=RPC,
        ipfs_gateways=f"{GW1},{GW2}",
        worker_poll_interval_s=0.1,
        job_lease_s=3,  # heartbeat every second
        _env_file=None,  # type: ignore[call-arg]
    )
    w = Worker(pool, settings, concurrency=1)
    task = asyncio.create_task(w.run())
    try:
        async with pool.acquire() as c:
            j = await queue.enqueue(c, "analyze", T22_MINT, "basic")
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(cancelled.wait(), 5)
        await asyncio.sleep(0.3)
        async with pool.acquire() as c:
            row = await c.fetchrow("select status, attempts from job where id=$1", j.id)
        # not released and not completed by the loser: the row is whoever owns it now
        assert row and row["status"] == "running" and row["attempts"] == 1
        assert j.id not in w.active_jobs
    finally:
        w.request_stop()
        await asyncio.wait_for(task, timeout=5)
        await pool.close()


# ----------------------------------------------------------------- analyzer: RT-1, RT-2


def _settings(db: str) -> Settings:
    return Settings(
        database_url=db,
        solana_rpc_url=RPC,
        ipfs_gateways=f"{GW1},{GW2}",
        _env_file=None,  # type: ignore[call-arg]
    )


def _web(
    r: respx.MockRouter, chain: FakeChain, *, meta_ok: bool, img_ok: bool
) -> tuple[list[int], list[int]]:
    meta_calls: list[int] = []
    img_calls: list[int] = []

    def meta(_req: httpx.Request) -> httpx.Response:
        meta_calls.append(1)
        if meta_ok:
            return httpx.Response(
                200, content=metadata_json(), headers={"content-type": "application/json"}
            )
        return httpx.Response(503)

    def img(_req: httpx.Request) -> httpx.Response:
        img_calls.append(1)
        if img_ok:
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(503)

    r.post(RPC).mock(side_effect=chain.handle)
    for gw in (GW1, GW2):
        r.get(f"{gw}/ipfs/{CID_META}").mock(side_effect=meta)
        r.get(f"{gw}/ipfs/{CID_IMG}").mock(side_effect=img)
    r.route().mock(return_value=httpx.Response(404))
    return meta_calls, img_calls


async def _run(
    conn: asyncpg.Connection, ctx: analyzer.Context, chain: FakeChain, **web: bool
) -> tuple[dict[str, Any], int, int]:
    with respx.mock(assert_all_called=False) as r:
        meta_calls, img_calls = _web(r, chain, **web)
        version = await analyzer.analyze(conn, ctx, T22_MINT, "basic")
    doc = await conn.fetchval(
        "select doc from analysis where mint=$1 and version=$2", T22_MINT, version
    )
    return doc, len(meta_calls), len(img_calls)


@needs_db
async def test_logo_that_failed_once_is_fetched_again_on_a_backoff(
    conn: asyncpg.Connection, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-1: metadata ok, logo 503 on the first read. The caveat stays while the logo is
    missing, the logo is not hammered on every read, and it is picked up when its retry is
    due."""
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    ctx = analyzer.Context.create(_settings(migrated_db))
    chain = _chain()
    try:
        doc, _, img = await _run(conn, ctx, chain, meta_ok=True, img_ok=False)
        assert img >= 1 and doc["image"]["status"] == "failed"
        assert any(c.startswith("image could not be fetched") for c in doc["caveats"])
        row = await conn.fetchrow(
            "select image_content_key, image_error, image_attempts, image_retry_at "
            "from token_metadata where mint=$1",
            T22_MINT,
        )
        assert row and row["image_content_key"] is None and row["image_attempts"] == 1
        assert row["image_error"] and row["image_retry_at"] is not None

        # the gateway is healthy now, but the retry is not due: no fetch, caveat repeated
        doc, meta, img = await _run(conn, ctx, chain, meta_ok=True, img_ok=True)
        assert meta == 0 and img == 0 and doc["image"]["status"] == "failed"
        assert any(c.startswith("image could not be fetched") for c in doc["caveats"])

        await conn.execute(
            "update token_metadata set image_retry_at = now() - interval '1 second' where mint=$1",
            T22_MINT,
        )
        doc, meta, img = await _run(conn, ctx, chain, meta_ok=True, img_ok=True)
        assert meta == 0 and img == 1
        assert doc["image"]["status"] == "ok" and doc["image"]["phash"]
        assert not any(c.startswith("image could not be fetched") for c in doc["caveats"])
        row = await conn.fetchrow(
            "select image_content_key, image_error, image_retry_at from token_metadata "
            "where mint=$1",
            T22_MINT,
        )
        assert row and row["image_content_key"] and row["image_error"] is None
        assert row["image_retry_at"] is None
        assert await conn.fetchval("select count(*) from image") == 1
        # a dead logo: the backoff grows, so a later read does not fetch it again at once
        await conn.execute(
            "update token_metadata set image_content_key = null where mint=$1", T22_MINT
        )
        await conn.execute("update token_metadata set image_attempts = 4 where mint=$1", T22_MINT)
        await _run(conn, ctx, chain, meta_ok=True, img_ok=False)
        delay = await conn.fetchval(
            "select extract(epoch from image_retry_at - now()) from token_metadata where mint=$1",
            T22_MINT,
        )
        assert delay is not None and 23 * 3600 < float(delay) <= 24 * 3600
    finally:
        await ctx.close()


@needs_db
async def test_unresolved_metadata_keeps_its_retry_schedule_and_becomes_final(
    conn: asyncpg.Connection, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-2: consumer-driven re-analyses do not spend the background retry slots, and once
    the schedule is spent the metadata is final (invalid): not partial any more."""
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    ctx = analyzer.Context.create(_settings(migrated_db))
    chain = _chain()
    try:
        for _ in range(3):
            doc, meta, _ = await _run(conn, ctx, chain, meta_ok=False, img_ok=True)
            assert meta >= 1
            assert any(str(c).startswith("partial:") for c in doc["caveats"])
            row = await conn.fetchrow(
                "select status, attempts, next_retry_at from token_metadata where mint=$1", T22_MINT
            )
            assert row and row["status"] == "unresolved" and row["attempts"] == 1
            assert row["next_retry_at"] is not None
        jobs = await conn.fetch("select kind, status from job where kind='retry_metadata'")
        assert [(j["kind"], j["status"]) for j in jobs] == [("retry_metadata", "pending")]

        # the chain ran out (as the last retry_metadata job leaves it)
        await conn.execute("update token_metadata set attempts = 7 where mint=$1", T22_MINT)
        doc, _, _ = await _run(conn, ctx, chain, meta_ok=False, img_ok=True)
        assert not any(str(c).startswith("partial:") for c in doc["caveats"])
        assert any(f["code"] == "metadata_unresolved" for f in doc["flags"])
        row = await conn.fetchrow(
            "select status, next_retry_at from token_metadata where mint=$1", T22_MINT
        )
        assert row and row["status"] == "invalid" and row["next_retry_at"] is None

        # the retry job itself ends the chain the same way, re-running the analysis once
        await conn.execute(
            "update token_metadata set status='unresolved', attempts = 6 where mint=$1", T22_MINT
        )
        with respx.mock(assert_all_called=False) as r:
            _web(r, chain, meta_ok=False, img_ok=True)
            rv = await analyzer.retry_metadata(conn, ctx, T22_MINT, "basic", job_id=None)
        assert rv is not None
        assert (
            await conn.fetchval("select status from token_metadata where mint=$1", T22_MINT)
        ) == "invalid"
        doc = await conn.fetchval(
            "select doc from analysis where mint=$1 and version=$2", T22_MINT, rv
        )
        assert not any(str(c).startswith("partial:") for c in doc["caveats"])
    finally:
        await ctx.close()


# ----------------------------------------------------------------- API


@needs_db
async def test_unknown_route_and_method_use_the_error_shape(client: httpx.AsyncClient) -> None:
    """API-1: a router 404/405 answers {"error": {...}} like every other error."""
    r = await client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
    r = await client.delete(f"/v1/tokens/{T22_MINT}")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in r.headers.get("allow", "")
    r = await client.post("/v1/meta")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


@needs_db
async def test_job_id_outside_int64_is_not_found(client: httpx.AsyncClient) -> None:
    """API-4 / SEC-5: a 20-digit job id is no job, not a 500."""
    for jid in ("99999999999999999999", "9223372036854775808", "0", "-1"):
        r = await client.get(f"/v1/jobs/{jid}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found", jid
        r = await client.post(f"/admin/v1/jobs/{jid}/retry", headers=ADMIN)
        assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found", jid


def test_openapi_declares_error_bodies() -> None:
    """API-7: every error entry carries the ErrorResponse schema; the batch route lists
    the errors it answers."""
    from tokensage.api.app import create_app

    o = create_app(Settings(_env_file=None)).openapi()  # type: ignore[call-arg]
    assert "ErrorResponse" in o["components"]["schemas"]
    ref = {"$ref": "#/components/schemas/ErrorResponse"}
    get = o["paths"]["/v1/tokens/{ca}"]["get"]["responses"]
    for code in ("400", "401", "404", "429", "503"):
        assert get[code]["content"]["application/json"]["schema"] == ref, code
    # 422 is FastAPI's own validation shape, so it is described but not given ErrorResponse
    assert get["422"]["description"] and "content" not in get["422"]
    batch = o["paths"]["/v1/tokens:batch"]["post"]["responses"]
    assert {"400", "401", "422", "429"} <= set(batch)
    assert batch["400"]["content"]["application/json"]["schema"] == ref
    retry = o["paths"]["/admin/v1/jobs/{job_id}/retry"]["post"]["responses"]
    assert retry["404"]["content"]["application/json"]["schema"] == ref
    committed = json.loads((ROOT / "openapi.v1.json").read_text())
    assert "ErrorResponse" in committed["components"]["schemas"]
    assert (
        "copy_of"
        in committed["components"]["schemas"]["Evidence"]["properties"]["where"]["description"]
    )


async def _age_doc(conn: asyncpg.Connection, mint: str, seconds: int) -> None:
    await conn.execute(
        """update analysis set doc = jsonb_set(doc, '{analyzed_at}', to_jsonb(
             to_char(now() - make_interval(secs => $2), 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')))
           where mint=$1""",
        mint,
        float(seconds),
    )


@needs_db
async def test_explicit_max_age_wins_over_the_just_finished_job_reuse(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """API-3: a document older than the caller's max_age is not served (as from_cache=false)
    just because the last job for the coin finished within 30 s."""
    install_web(router, _chain())
    conn = await asyncpg.connect(migrated_db)
    try:
        async with make_client(migrated_db) as c:
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 10})
            assert r.status_code == 200 and r.json()["status"] == "complete", r.text
            first_job = await conn.fetchval("select max(id) from job")
            await _age_doc(conn, T22_MINT, 25)
            await conn.execute("update job set finished_at = now()")
            r = await c.get(
                f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 0, "max_age": 60}
            )
            assert r.status_code == 200 and r.json()["freshness"]["from_cache"] is True
            r = await c.get(
                f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 0, "max_age": 10}
            )
            assert r.status_code == 202, r.text
            assert r.json()["status"] == "pending" and r.json()["job_id"] != first_job
    finally:
        await conn.close()


@needs_db
async def test_failure_while_waiting_has_the_same_shape_as_a_recent_failure(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """API-5: status=failed carries stale_analysis and the retry hint whether the job failed
    while this request waited or before it arrived."""
    install_web(router, _chain())
    conn = await asyncpg.connect(migrated_db)
    try:
        async with make_client(migrated_db) as c:
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 10})
            assert r.status_code == 200, r.text
        await _age_doc(conn, T22_MINT, 5000)
        async with make_client(migrated_db, inline_analyzer=False) as c:

            async def fail_soon() -> None:
                await asyncio.sleep(0.7)
                j = await queue.claim(conn, lease_s=60)
                assert j is not None
                await queue.fail(conn, j.id, "boom2", max_attempts=1, lease_token=j.lease_token)

            side = asyncio.create_task(fail_soon())
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 5})
            await side
            assert r.status_code == 200, r.text
            waited = r.json()
            r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 0})
            later = r.json()
        for body in (waited, later):
            assert body["status"] == "failed" and body["stale_analysis"] is not None
            assert body["errors"][0]["detail"].startswith("boom2; retried automatically after")
        assert waited["job_id"] == later["job_id"]
    finally:
        await conn.close()


@needs_db
async def test_hinted_request_is_not_answered_from_a_hintless_404(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """RT-3: a POST with metadata hints is analysed (partial) although a hint-less GET
    just got 404, without spending refresh quota."""
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        r = await c.get(f"/v1/tokens/{MISSING}", params={"wait": 5})
        assert r.status_code == 404 and r.json()["error"]["code"] == "token_not_found"
        r = await c.post(f"/v1/tokens/{MISSING}", params={"wait": 5}, json={"hints": HINTS})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "partial"
        assert r.headers["x-quota-refresh-remaining"] == "200"
        # the batch form too (same depth as the GET, so the same cooldown row)
        b = await c.post(
            "/v1/tokens:batch", json={"items": [{"ca": MISSING, "hints": HINTS}], "depth": "full"}
        )
        assert b.status_code == 200 and b.json()["items"][0]["status"] == "partial", b.text


@needs_db
async def test_token_not_found_is_held_for_seconds_not_minutes(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """RT-3: a seconds-old mint that appears on-chain after an early 404 is found by the
    documented 'retry once after a few seconds', not 404 for the next 10 minutes."""
    chain = _chain()
    install_web(router, chain)
    conn = await asyncpg.connect(migrated_db)
    try:
        async with make_client(migrated_db) as c:
            r = await c.get(f"/v1/tokens/{MISSING}", params={"wait": 5})
            assert r.status_code == 404
            chain.add_t22_pump(MISSING, "dog wif cap", "cap", META_URI)
            r = await c.get(f"/v1/tokens/{MISSING}", params={"wait": 5})
            assert r.status_code == 404  # within the short cooldown: still the cached answer
            await conn.execute(
                "update job set finished_at = now() - interval '25 seconds' "
                "where error_code='token_not_found'"
            )
            r = await c.get(f"/v1/tokens/{MISSING}", params={"wait": 10})
            assert r.status_code == 200 and r.json()["status"] == "complete", r.text
    finally:
        await conn.close()


# ----------------------------------------------------------------- static pages, CI, SEC-1


def test_console_retries_the_original_url_after_a_429() -> None:
    """API-9: the back-off branch keeps refresh/max_age (nothing was enqueued)."""
    src = (STATIC / "index.html").read_text()
    branch = src[src.index("resp.status === 429 || resp.status === 503") :]
    branch = branch[: branch.index("continue;")]
    assert "first = false" not in branch


def test_admin_chart_does_not_count_a_429_twice() -> None:
    """API-8: errors already include the 429s; the bad band is errors alone."""
    src = (STATIC / "admin.html").read_text()
    assert "p.rate_limited + p.errors" not in src
    assert "values: [p.credits, p.errors]" in src


def test_require_db_flag_fails_the_run_when_postgres_is_unreachable() -> None:
    """TDD-3: with TS_REQUIRE_DB=1 (CI) an unreachable Postgres is a failed run, not a
    green run of skips."""
    url = "postgresql://tokensage:tokensage@localhost:1/nope"
    env = {**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url, "TS_REQUIRE_DB": "1"}
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_queue.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode != 0, res.stdout + res.stderr
    assert "TS_REQUIRE_DB=1" in res.stdout + res.stderr


async def test_wiki_lookup_text_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-1: post text handed to the capitalised-span regex is bounded."""
    from tokensage.engine import wikilookup
    from tokensage.engine.pipeline import EngineInput
    from tokensage.sources.x import TweetData

    seen: list[list[str]] = []

    def spans(_n: Any, texts: list[str], *_a: Any) -> list[Any]:
        seen.append(list(texts))
        return []

    monkeypatch.setattr(wikilookup, "spans", spans)
    long = "Ab" * 12500
    tweet = TweetData(id="1", status="ok", text=long)
    tweet.quoted = TweetData(id="2", status="ok", text=long)
    inp = EngineInput(
        mint=T22_MINT,
        name="Test Coin",
        symbol="TST",
        description=None,
        image_bytes=None,
        created_at=None,
        tweet=tweet,
    )
    refs = await analyzer._wiki_refs(None, None, inp)  # type: ignore[arg-type]
    assert refs == []
    assert seen and len(seen[0]) == 2
    assert all(len(t) == analyzer.WIKI_TEXT_MAX_CHARS for t in seen[0])
