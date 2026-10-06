"""End to end at depth=full: fake chain + fake gateways + fake FxTwitter + fake Wikimedia
trend rows, through the API and the inline worker, with caching checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import (
    CID_META,
    T22_MINT,
    FakeChain,
    install_web,
    metadata_json,
    public_resolver,
)
from tests.test_x_sources import FX_OK, TID
from tokensage.api.schemas import TokenResponse
from tokensage.net import safe_fetch
from tokensage.sources import x as xs

pytestmark = needs_db
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await conn.execute(
        "truncate token_metadata, image, x_ref, x_tweet, x_profile, x_profile_history, "
        "trend_term, lookup_cache, token_market cascade"
    )
    await conn.execute(
        """insert into trend_term (term, source, score, spike, first_seen, day, views)
           values ('Peanut (squirrel)', 'wikipedia', 900000, 40.0, $1, $1, 900000)""",
        date.today(),
    )
    from tokensage import fulldepth

    fulldepth._trend_cache = None
    try:
        yield conn
    finally:
        await conn.close()


RSS = "<rss><channel><item><title>Peanut the squirrel story</title></item></channel></rss>"


def _install_x(router: respx.MockRouter, fx_calls: list[int]) -> None:
    def fx(request: httpx.Request) -> httpx.Response:
        fx_calls.append(1)
        return httpx.Response(200, json=FX_OK)

    router.get(f"{xs.FX}/status/{TID}").mock(side_effect=fx)
    router.get(f"{xs.FX}/profile/elonmusk").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "user": {
                    "id": "44196397",
                    "screen_name": "elonmusk",
                    "name": "Elon Musk",
                    "followers": 190000000,
                    "joined": "2009-06-02T20:12:29.000Z",
                    "verification": {"verified": True},
                    "about_account": {"username_changes": {"count": 0}},
                },
            },
        )
    )
    router.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(
            200,
            text=RSS,
        )
    )


async def test_full_depth_fills_x_trend_and_caches(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Peanut the Squirrel 2.0", "PNUT2", META_URI, progress=0.3)
    # make the fake token "created" after the tweet (FX_OK created 2024-05-15) -> narrative source
    fx_calls: list[int] = []
    meta = metadata_json(
        name="Peanut the Squirrel 2.0",
        symbol="PNUT2",
        description="rip peanut 🐿",
        twitter=f"https://x.com/elonmusk/status/{TID}",
    )
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, fx_calls)  # before install_web: its catch-all 404 must come last
        install_web(router, chain, meta=meta)
        r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10")
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.depth == "full"
    # X content
    assert a.x is not None and a.x.status == "ok" and a.x.fetch_source == "fxtwitter"
    assert a.x.author and a.x.author.handle == "elonmusk" and a.x.author.followers == 190000000
    assert a.x.text and "Peanut" in a.x.text
    # the fake token's created_at is null (no history), so timing-based relation is launch/official;
    # with no created_at the engine cannot claim narrative; at least no spoof is raised
    codes = {f.code for f in a.flags}
    assert "spoofed_tweet_handle" not in codes
    # trend
    assert a.trend.matched and a.trend.terms[0].term == "Peanut (squirrel)"
    assert any("news check" in c for c in a.caveats)
    # meaning
    assert a.referent and "Peanut" in a.referent.label
    assert "derivative/sequel" in {c.label for c in a.categories}
    # caches populated
    assert await db.fetchval("select status from x_tweet where tweet_id=$1", TID) == "ok"
    assert await db.fetchval("select count(*) from x_profile") == 1
    assert await db.fetchval("select count(*) from lookup_cache") == 1
    assert len(fx_calls) == 1

    # second full analysis: tweet comes from cache, FxTwitter not called again
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, fx_calls)
        install_web(router, chain, meta=meta)
        r2 = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&refresh=true&wait=10")
    assert r2.status_code == 200 and len(fx_calls) == 1


async def test_basic_depth_does_not_fetch_x(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI)
    fx_calls: list[int] = []
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, fx_calls)
        install_web(router, chain)
        r = await client.get(f"/v1/tokens/{T22_MINT}?depth=basic&wait=10")
    assert r.status_code == 200
    a = TokenResponse.model_validate(r.json()).analysis
    assert a and a.x and a.x.status == "not_fetched" and not fx_calls
    assert not a.trend.matched
