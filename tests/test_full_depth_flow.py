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
        "trend_term, lookup_cache, token_market, x_media cascade"
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


QUOTE_TID = "1850000000000000002"
QUOTED_TID = "1790000000000000001"


def _launch_quote(inline: bool) -> dict:  # type: ignore[type-arg]
    quote: dict = {"id": QUOTED_TID}  # type: ignore[type-arg]
    if inline:
        quote.update(
            text="Peanut the squirrel deserved better",
            created_timestamp=1727773200,
            author={"id": "44196397", "screen_name": "elonmusk", "followers": 190000000},
        )
    return {
        "code": 200,
        "status": {
            "id": QUOTE_TID,
            "text": "launching $PNUT2",
            "created_timestamp": 1727787600,
            "author": {"id": "9", "screen_name": "nutdev", "followers": 40},
            "quote": quote,
        },
    }


@pytest.mark.parametrize("inline", [True, False], ids=["inline_quote", "quote_fetched_by_id"])
async def test_full_depth_includes_the_quoted_tweet(
    client: httpx.AsyncClient, db: asyncpg.Connection, inline: bool
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Peanut the Squirrel 2.0", "PNUT2", META_URI, progress=0.3)
    meta = metadata_json(
        name="Peanut the Squirrel 2.0",
        symbol="PNUT2",
        description="rip peanut",
        twitter=f"https://x.com/nutdev/status/{QUOTE_TID}",
    )
    quoted_calls: list[int] = []

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{xs.FX}/status/{QUOTE_TID}").mock(
            return_value=httpx.Response(200, json=_launch_quote(inline))
        )
        router.get(f"{xs.FX}/status/{QUOTED_TID}").mock(
            side_effect=lambda req: (
                quoted_calls.append(1),
                httpx.Response(
                    200, json={"code": 200, "status": _launch_quote(True)["status"]["quote"]}
                ),
            )[1]
        )
        router.get(url__startswith=f"{xs.FX}/profile/").mock(return_value=httpx.Response(404))
        router.get(url__startswith=xs.VX).mock(return_value=httpx.Response(404))
        router.get(url__startswith="https://news.google.com/rss/search").mock(
            return_value=httpx.Response(200, text=RSS)
        )
        install_web(router, chain, meta=meta)
        r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10")
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.x is not None and a.x.status == "ok"
    assert a.x.author and a.x.author.handle == "nutdev"
    q = a.x.quoted
    assert q is not None and q.status == "ok" and q.id == QUOTED_TID
    assert q.author and q.author.handle == "elonmusk" and q.author.followers == 190000000
    assert q.text == "Peanut the squirrel deserved better"
    assert q.url == f"https://x.com/elonmusk/status/{QUOTED_TID}"
    assert q.created_at is not None
    # inline quotes cost no extra request; an id-only quote is fetched once and cached
    assert len(quoted_calls) == (0 if inline else 1)
    if not inline:
        assert await db.fetchval("select status from x_tweet where tweet_id=$1", QUOTED_TID) == "ok"


MEDIA_URL = "https://pbs.twimg.com/media/LOGOCOPY.jpg"


async def test_full_depth_compares_post_media_with_the_logo(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    from tests.fixtures.chain import PNG

    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.3)
    meta = metadata_json(twitter=f"https://x.com/capdev/status/{QUOTE_TID}")
    post = {
        "code": 200,
        "status": {
            "id": QUOTE_TID,
            "text": "dog wif cap is live $cap",
            "created_timestamp": 1727787600,
            "author": {"id": "9", "screen_name": "capdev", "followers": 40},
            "media": {"photos": [{"url": MEDIA_URL}]},
        },
    }
    media_calls: list[int] = []

    def media(request: httpx.Request) -> httpx.Response:
        media_calls.append(1)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    for attempt in range(2):
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{xs.FX}/status/{QUOTE_TID}").mock(
                return_value=httpx.Response(200, json=post)
            )
            router.get(url__startswith=MEDIA_URL).mock(side_effect=media)
            router.get(url__startswith=f"{xs.FX}/profile/").mock(return_value=httpx.Response(404))
            router.get(url__startswith=xs.VX).mock(return_value=httpx.Response(404))
            router.get(url__startswith="https://news.google.com/rss/search").mock(
                return_value=httpx.Response(200, text=RSS)
            )
            install_web(router, chain, meta=meta)
            extra = "&refresh=true" if attempt else ""
            r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10{extra}")
        assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.x is not None and a.x.match is not None
    m = a.x.match
    assert m.image.media_checked == 1 and m.image.best_distance == 0 and m.image.score == 1.0
    assert m.name.how == "exact" and m.ticker.how == "cashtag"
    assert m.verdict == "about_this_coin" and m.fit >= 0.95
    codes = {f.code for f in a.flags}
    assert "x_image_match" in codes and "x_content_mismatch" not in codes
    # the media hash is cached by URL: the second analysis did not download it again
    assert len(media_calls) == 1
    assert await db.fetchval("select status from x_media where url=$1", MEDIA_URL) == "ok"


async def test_unrelated_post_is_flagged(client: httpx.AsyncClient, db: asyncpg.Connection) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI, progress=0.3)
    meta = metadata_json(twitter=f"https://x.com/someone/status/{QUOTE_TID}")
    post = {
        "code": 200,
        "status": {
            "id": QUOTE_TID,
            "text": "Quarterly earnings call moved to Thursday",
            "created_timestamp": 1727787600,
            "author": {"id": "9", "screen_name": "someone", "followers": 400},
        },
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{xs.FX}/status/{QUOTE_TID}").mock(return_value=httpx.Response(200, json=post))
        router.get(url__startswith=f"{xs.FX}/profile/").mock(return_value=httpx.Response(404))
        router.get(url__startswith=xs.VX).mock(return_value=httpx.Response(404))
        router.get(url__startswith="https://news.google.com/rss/search").mock(
            return_value=httpx.Response(200, text=RSS)
        )
        install_web(router, chain, meta=meta)
        r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10")
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.x is not None and a.x.match is not None
    assert a.x.match.verdict == "unrelated" and a.x.match.fit < 0.2
    assert a.x.match.image.media_checked == 0
    assert "x_content_mismatch" in {f.code for f in a.flags}
