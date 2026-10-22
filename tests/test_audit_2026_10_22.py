"""Regression tests for the 2026-10-22 audit: each reproduces a verified bug."""

from __future__ import annotations

import asyncpg
import respx

from tests.conftest import API_KEY, needs_db
from tests.fixtures.chain import CID_META, T22_MINT, FakeChain, install_web
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage.api.schemas import TokenResponse

# ------------------------------------------------- TS-1: a trusted key's hinted reads count

HINTS = {
    "name": "dog wif cap",
    "symbol": "cap",
    "description": "description from the caller",
    "twitter": "https://x.com/elonmusk/status/1791351500217754008",
    "created_at": "2026-10-01T12:00:00Z",
}
PEER_KEY = "peer-key-0123456789abcdef"


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", f"https://ipfs.io/ipfs/{CID_META}")
    return c


async def _shared_rows(db: str) -> tuple[int, int]:
    conn = await asyncpg.connect(db)
    try:
        xref = await conn.fetchval("select count(*) from x_ref where mint=$1", T22_MINT)
        read = await conn.fetchval("select count(*) from token_read where mint=$1", T22_MINT)
    finally:
        await conn.close()
    return xref, read


@needs_db
async def test_trusted_key_hints_write_xref_and_token_read(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """TrenchScanner hints every read, so with SG-003 alone no coin it read reached x_ref or
    token_read: X-reuse counts and launch waves fell to about zero."""
    install_web(router, _chain())
    keys = f"tester:{API_KEY},peer:{PEER_KEY}"
    async with make_client(migrated_db, api_keys=keys, trusted_hint_keys="tester") as c:
        r = await c.post(f"/v1/tokens/{T22_MINT}", params={"wait": 5}, json={"hints": HINTS})
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.raw.description == "description from the caller"
    assert await _shared_rows(migrated_db) == (1, 1)
    # and the read is an ordinary cache hit for another key
    async with make_client(migrated_db, api_keys=keys, bearer=PEER_KEY) as peer:
        g = await peer.get(f"/v1/tokens/{T22_MINT}", params={"wait": 5})
    assert g.status_code == 200, g.text
    peer_a = TokenResponse.model_validate(g.json()).analysis
    assert peer_a is not None and peer_a.raw.description == "description from the caller"


@needs_db
async def test_untrusted_key_hints_stay_private(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db, trusted_hint_keys="trenchscanner") as c:
        r = await c.post(f"/v1/tokens/{T22_MINT}", params={"wait": 5}, json={"hints": HINTS})
    assert r.status_code == 200, r.text
    assert await _shared_rows(migrated_db) == (0, 0)
