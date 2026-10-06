"""Caller-supplied metadata hints: skip the metadata fetch, never 404 a not-yet-visible mint,
treat every value as untrusted."""

from __future__ import annotations

import asyncio

import httpx
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import (
    CID_IMG,
    CID_META,
    MISSING,
    T22_MINT,
    FakeChain,
    install_web,
    metadata_json,
)
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage.api.schemas import TokenResponse

pytestmark = needs_db
META_URI = f"https://ipfs.io/ipfs/{CID_META}"
IMG = f"https://ipfs.io/ipfs/{CID_IMG}"
HINTS = {
    "name": "dog wif cap",
    "symbol": "cap",
    "description": "description from the caller",
    "image_url": IMG,
    "twitter": "https://x.com/dogwifcap",
    "created_at": "2026-10-01T12:00:00Z",
}


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.4)
    return c


def _count_meta(router: respx.MockRouter) -> list[int]:  # noqa: F811
    calls: list[int] = []

    def meta(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=metadata_json())

    # registered first, so it wins over install_web's metadata route on every gateway
    router.get(url__regex=rf".*/ipfs/{CID_META}$").mock(side_effect=meta)
    return calls


async def test_hints_skip_the_metadata_fetch(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    meta_calls = _count_meta(router)
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        r = await c.post(f"/v1/tokens/{T22_MINT}", params={"wait": 5}, json={"hints": HINTS})
    assert r.status_code == 200, r.text
    body = TokenResponse.model_validate(r.json())
    a = body.analysis
    assert body.status == "complete" and a is not None
    assert meta_calls == []  # no IPFS metadata round trip
    assert a.raw.description == "description from the caller"
    assert a.raw.twitter == "https://x.com/dogwifcap"
    assert a.image.status == "ok" and a.image.phash  # the hinted image was fetched and hashed
    assert "hints: metadata supplied by caller" in a.caveats
    prov = [e for e in a.evidence if e.kind == "provenance"]
    assert prov and prov[0].source == "hints:caller" and "description" in prov[0].detail
    assert any(e.source == "solana-rpc" for e in a.evidence)  # still verified on-chain
    assert a.created_at is not None and a.created_at.isoformat().startswith("2026-10-01T12:00")


async def test_mint_not_yet_on_chain_is_analysed_from_hints(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        plain = await c.get(f"/v1/tokens/{MISSING}", params={"wait": 5})
        assert plain.status_code == 404  # without hints nothing changes
        r = await c.post(
            f"/v1/tokens/{MISSING}",
            params={"wait": 5, "refresh": "true"},
            json={"hints": {**HINTS, "name": "Peanut the Squirrel 2.0", "symbol": "PNUT2"}},
        )
    assert r.status_code == 200, r.text
    body = TokenResponse.model_validate(r.json())
    a = body.analysis
    assert body.status == "partial" and a is not None
    assert a.raw.name == "Peanut the Squirrel 2.0" and a.raw.symbol == "PNUT2"
    assert any(c.startswith("partial: mint not yet visible on-chain") for c in a.caveats)
    assert not any(e.source == "solana-rpc" for e in a.evidence)
    assert a.referent and "Peanut" in a.referent.label  # the engine still ran
    assert a.created_at is not None


async def test_onchain_name_wins_over_a_different_hint(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        r = await c.post(
            f"/v1/tokens/{T22_MINT}",
            params={"wait": 5},
            json={"hints": {**HINTS, "name": "Totally Different"}},
        )
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.raw.name == "dog wif cap"
    assert any("differs from on-chain" in c for c in a.caveats)


async def test_unsafe_hint_urls_are_refused(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    probe = router.get(url__startswith="http://127.0.0.1").mock(return_value=httpx.Response(200))
    install_web(router, _chain())
    async with make_client(migrated_db) as c:
        r = await c.post(
            f"/v1/tokens/{T22_MINT}",
            params={"wait": 5},
            json={"hints": {**HINTS, "image_url": "http://127.0.0.1/admin.png"}},
        )
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.image.status != "ok"
    assert not probe.called


async def test_batch_items_carry_hints(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db, rate_per_min_default=10_000) as c:
        r = await c.post(
            "/v1/tokens:batch",
            json={"items": [{"ca": MISSING, "hints": HINTS}], "cas": ["not-a-ca"]},
        )
        assert r.status_code == 200, r.text
        bad, item = r.json()["items"]
        assert bad["status"] == "invalid"
        assert item["status"] == "pending" and item["job_id"]
        for _ in range(100):
            j = (await c.get(f"/v1/jobs/{item['job_id']}")).json()
            if j["status"] in ("done", "failed"):
                break
            await asyncio.sleep(0.1)
    assert j["status"] == "done", j
    assert j["result"]["status"] == "partial"
    assert j["result"]["analysis"]["raw"]["description"] == "description from the caller"


async def test_batch_needs_at_least_one_ca(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    async with make_client(migrated_db, inline_analyzer=False) as c:
        r = await c.post("/v1/tokens:batch", json={"items": []})
    assert r.status_code in (400, 422)
