"""End to end through the API with a fake chain and fake gateways: real resolver, real
metadata fetcher, real worker (inline), real Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import (
    CID_IMG,
    CID_META,
    MISSING,
    SPL_MINT,
    T22_MINT,
    USDC,
    WALLET,
    FakeChain,
    install_web,
    public_resolver,
)
from tokensage.api.schemas import TokenResponse
from tokensage.config import Settings
from tokensage.net import safe_fetch

pytestmark = needs_db
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


@pytest.fixture
def chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.4)
    c.add_spl_pump(SPL_MINT, "StreamerCoin", "STREAMER", META_URI, complete=True)
    c.add_plain_spl(USDC)
    c.add_wallet(WALLET)
    return c


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await conn.execute("truncate token_metadata, image, x_ref, token_market cascade")
    try:
        yield conn
    finally:
        await conn.close()


async def _get(client: httpx.AsyncClient, ca: str, **params: object) -> httpx.Response:
    # these tests cover resolution; depth=basic keeps X/trend fetching out of the picture
    return await client.get(f"/v1/tokens/{ca}", params={"wait": 8, "depth": "basic", **params})


async def test_token2022_pump_coin_full_resolution(
    client: httpx.AsyncClient, chain: FakeChain, db: asyncpg.Connection
) -> None:
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r = await _get(client, T22_MINT)
    assert r.status_code == 200, r.text
    body = TokenResponse.model_validate(r.json())
    a = body.analysis
    assert body.status == "complete" and a is not None
    assert a.launchpad == "pump.fun"
    assert a.raw.name == "dog wif cap" and a.raw.symbol == "cap"
    assert a.raw.description == "just a dog wif a cap"
    assert a.raw.website is None  # javascript: scrubbed
    assert a.raw.telegram == "https://t.me/dogwifcap"
    assert a.market.complete is False and a.market.curve_progress == pytest.approx(0.4, abs=0.01)
    assert a.market.creator and a.market.quote_mint == "SOL"
    assert a.image.status == "ok" and a.image.source_url and CID_IMG in a.image.source_url
    assert a.x is not None and a.x.ref.kind == "tweet"
    assert a.x.ref.tweet_id == "1791351500217754008" and a.x.ref.url_handle == "elonmusk"
    assert a.x.object_time is not None and a.x.status == "not_fetched"
    assert "metadata_unresolved" not in {f.code for f in a.flags}

    # persisted state
    tok = await db.fetchrow("select * from token where mint=$1", T22_MINT)
    assert tok and tok["is_pumpfun"] and tok["token_program"] == "token-2022"
    assert tok["uri"] == META_URI and tok["launcher"] == "https://pump.fun"
    meta = await db.fetchrow("select * from token_metadata where mint=$1", T22_MINT)
    assert meta and meta["status"] == "ok" and meta["content_key"] == f"ipfs:{CID_META}"
    img = await db.fetchrow("select * from image where content_key=$1", f"ipfs:{CID_IMG}")
    assert img and img["mime"] == "image/png"
    xr = await db.fetchrow("select * from x_ref where mint=$1", T22_MINT)
    assert xr and xr["tweet_id"] == "1791351500217754008"

    # second request: metadata cache hit, no gateway calls
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain, gateways_ok=False)
        r2 = await _get(client, T22_MINT, refresh="true")
    assert r2.status_code == 200 and r2.json()["analysis"]["raw"]["description"]


async def test_legacy_spl_pump_coin_uses_metaplex(
    client: httpx.AsyncClient, chain: FakeChain, db: asyncpg.Connection
) -> None:
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r = await _get(client, SPL_MINT)
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a and a.launchpad == "pump.fun" and a.market.complete is True
    assert a.market.curve_progress == 1.0
    tok = await db.fetchrow("select * from token where mint=$1", SPL_MINT)
    assert tok and tok["token_program"] == "spl-token" and tok["name"] == "StreamerCoin"
    assert tok["uri"] == META_URI
    # mint, bonding curve and Metaplex PDA come from one getMultipleAccounts call; only the
    # pump.fun Global account (read once per process) may still use getAccountInfo
    assert chain.calls.count("getMultipleAccounts") == 1
    assert chain.calls.count("getAccountInfo") <= 1 and "getAsset" not in chain.calls


async def test_non_pump_mint_is_best_effort_by_default(
    client: httpx.AsyncClient, chain: FakeChain, db: asyncpg.Connection
) -> None:
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r = await _get(client, USDC)
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a and a.launchpad == "unknown"
    assert "non_pumpfun" in {f.code for f in a.flags}
    assert "metadata_unresolved" in {f.code for f in a.flags}  # no uri at all


async def test_wallet_and_missing_map_to_422_and_404(
    client: httpx.AsyncClient, chain: FakeChain, db: asyncpg.Connection
) -> None:
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r1 = await _get(client, WALLET)
        r2 = await _get(client, MISSING)
    assert r1.status_code == 422 and r1.json()["error"]["code"] == "not_a_token_mint"
    assert r2.status_code == 404 and r2.json()["error"]["code"] == "token_not_found"
    # definitive failures are not retried
    row = await db.fetchrow("select status, attempts, error_code from job where mint=$1", WALLET)
    assert row and row["status"] == "failed" and row["attempts"] == 1
    assert row["error_code"] == "not_a_token_mint"


async def test_reject_non_pump_when_configured(
    migrated_db: str, chain: FakeChain, db: asyncpg.Connection
) -> None:
    from tests.conftest import API_KEY
    from tokensage.api.app import create_app

    settings = Settings(
        database_url=migrated_db,
        api_keys=f"tester:{API_KEY}",
        inline_analyzer=True,
        solana_rpc_url="https://rpc.test/",
        ipfs_gateways="https://gw1.test,https://gw2.test",
        accept_non_pump=False,
        worker_poll_interval_s=0.2,
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as client,
    ):
        with respx.mock(assert_all_called=False) as router:
            install_web(router, chain)
            r = await _get(client, USDC)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_pumpfun"


async def test_metadata_outage_gives_partial_and_schedules_retry(
    client: httpx.AsyncClient, chain: FakeChain, db: asyncpg.Connection
) -> None:
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain, gateways_ok=False)
        r = await _get(client, T22_MINT)
    assert r.status_code == 200, r.text
    body = TokenResponse.model_validate(r.json())
    a = body.analysis
    assert body.status == "partial" and a is not None
    assert a.raw.name == "dog wif cap"  # from on-chain
    assert a.raw.description is None
    assert "metadata_unresolved" in {f.code for f in a.flags}
    meta = await db.fetchrow("select * from token_metadata where mint=$1", T22_MINT)
    assert meta and meta["status"] == "unresolved" and meta["attempts"] == 1
    assert meta["next_retry_at"] is not None
    job = await db.fetchrow("select * from job where kind='retry_metadata' and mint=$1", T22_MINT)
    assert job and job["status"] == "pending"

    # make the retry due now and let the (inline) worker pick it up with gateways back
    await db.execute("update job set run_after = now() where id=$1", job["id"])
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain, gateways_ok=True)
        await db.execute("select pg_notify('job_new', $1)", str(job["id"]))
        for _ in range(60):
            st = await db.fetchval("select status from job where id=$1", job["id"])
            if st == "done":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("retry job did not run")
    meta2 = await db.fetchrow("select * from token_metadata where mint=$1", T22_MINT)
    assert meta2 and meta2["status"] == "ok"
    n = await db.fetchval("select count(*) from analysis where mint=$1", T22_MINT)
    assert n == 2  # a new version after the retry succeeded
