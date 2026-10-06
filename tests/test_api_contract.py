"""The /v1 contract: auth, validation, errors, and the wait-or-202 flow with a real worker."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from pydantic import ValidationError

from tests.conftest import ADMIN_KEY, needs_db
from tests.fixtures.chain import CID_META, SPL_MINT, FakeChain, install_web, public_resolver
from tokensage.api.schemas import Analysis, MetaResponse, TokenResponse
from tokensage.net import safe_fetch
from tokensage.taxonomy import category_labels, flag_codes

CA = SPL_MINT
pytestmark = needs_db


@pytest.fixture(autouse=True)
def _fake_world(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """The analyzer is real now: give it a fake chain + fake gateways for CA."""
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    chain = FakeChain()
    chain.add_spl_pump(SPL_MINT, "StreamerCoin", "STREAMER", f"https://ipfs.io/ipfs/{CID_META}")
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        yield


async def test_healthz_no_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"Authorization": ""})
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_missing_key_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/tokens/{CA}", headers={"Authorization": ""})
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthorized" and body["request_id"]


async def test_wrong_key_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/tokens/{CA}", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.parametrize("bad", ["hello", "0" * 44, "1" * 50, CA[:-1] + "0"])
async def test_invalid_ca_is_400_before_any_work(client: httpx.AsyncClient, bad: str) -> None:
    r = await client.get(f"/v1/tokens/{bad}")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_ca"


async def test_full_flow_returns_schema_valid_analysis(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/tokens/{CA}?wait=5")
    assert r.status_code == 200, r.text
    body = TokenResponse.model_validate(r.json())
    assert body.status == "complete" and body.analysis is not None
    assert body.analysis.mint == CA and body.analysis.depth == "full"
    assert body.freshness.from_cache is False
    assert r.headers["X-Request-Id"] == body.request_id

    # second call is a cache hit
    r2 = await client.get(f"/v1/tokens/{CA}")
    b2 = TokenResponse.model_validate(r2.json())
    assert r2.status_code == 200 and b2.freshness.from_cache is True

    # refresh forces a new version
    r3 = await client.get(f"/v1/tokens/{CA}?refresh=true&wait=5")
    b3 = TokenResponse.model_validate(r3.json())
    assert r3.status_code == 200 and b3.freshness.from_cache is False


async def test_wait_zero_gives_202_then_job_completes(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/tokens/{CA}?wait=0&depth=basic")
    assert r.status_code == 202, r.text
    body = TokenResponse.model_validate(r.json())
    assert body.status == "pending" and body.job_id is not None
    assert r.headers.get("Retry-After")

    for _ in range(50):
        j = await client.get(f"/v1/jobs/{body.job_id}")
        assert j.status_code == 200
        if j.json()["status"] == "done":
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("job never completed")
    assert j.json()["result"]["analysis"]["mint"] == CA

    # a basic-depth result does not satisfy a full-depth request, so a new job is queued
    r2 = await client.get(f"/v1/tokens/{CA}?wait=0")
    assert r2.status_code == 202
    assert r2.json()["stale_analysis"] is None


async def test_concurrent_requests_share_one_job(client: httpx.AsyncClient) -> None:
    rs = await asyncio.gather(
        *[client.get(f"/v1/tokens/{CA}?wait=0&depth=full") for _ in range(20)]
    )
    ids = {r.json()["job_id"] for r in rs}
    assert len(ids) == 1, ids


async def test_batch(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/tokens:batch", json={"cas": [CA, "garbage"], "depth": "basic"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["status"] == "pending" and items[0]["job_id"]
    assert items[1]["status"] == "invalid"


async def test_job_not_found(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/jobs/999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found"


async def test_meta_matches_taxonomy_file(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/meta")
    assert r.status_code == 200
    m = MetaResponse.model_validate(r.json())
    assert {c.label for c in m.categories} == category_labels()
    assert {f.code for f in m.flags} == flag_codes()
    assert "not financial advice" in m.disclaimer


async def test_readyz_requires_admin(client: httpx.AsyncClient) -> None:
    assert (await client.get("/readyz")).status_code == 403
    r = await client.get("/readyz", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.status_code == 200 and "queue" in r.json()


async def test_openapi_exposes_analysis_schema(client: httpx.AsyncClient) -> None:
    r = await client.get("/openapi.json", headers={"Authorization": ""})
    schema = r.json()
    assert "Analysis" in schema["components"]["schemas"]
    assert "/v1/tokens/{ca}" in schema["paths"]


def test_analysis_schema_forbids_unknown_fields() -> None:
    from tokensage.analyzer import build_document
    from tokensage.resolve.resolver import Resolved

    r = Resolved(
        mint=CA,
        token_program="spl-token",
        is_pumpfun=True,
        name="n",
        symbol="s",
        uri=None,
        creator=None,
        bonding_curve=None,
        complete=False,
        curve_progress=0.1,
        is_mayhem=False,
        quote_mint="SOL",
        created_at=None,
        created_at_source=None,
        onchain_metadata_source="none",
    )
    doc = build_document(r, None, "basic", None, None).model_dump(mode="json")
    Analysis.model_validate(doc)
    doc["bogus"] = 1
    with pytest.raises(ValidationError):
        Analysis.model_validate(doc)


async def test_console_served_without_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/", headers={"Authorization": ""})
    assert r.status_code == 200 and "TokenSage Console" in r.text
    assert r.headers["content-type"].startswith("text/html")
    assert (await client.get("/console", headers={"Authorization": ""})).status_code == 200
