"""Regression tests for the 2026-10-23 external-API audit: each fix saves upstream calls and
leaves the outputs as they were."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import CID_IMG, PNG, T22_MINT, public_resolver
from tests.test_audit_0_28_runtime import _chain, _settings, _web, conn  # noqa: F401
from tests.test_hints import HINTS as _HINTS
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage import analyzer, fulldepth, now
from tokensage.api.schemas import StoryHeat, TokenResponse
from tokensage.config import Settings
from tokensage.engine import ocr
from tokensage.net import safe_fetch
from tokensage.net.breaker import CircuitBreaker, breaker
from tokensage.net.safe_fetch import Fetched
from tokensage.resolve import metadata as md
from tokensage.resolve import resolver
from tokensage.sources import geckoterminal
from tokensage.sources import x as xs

HINTS = {k: v for k, v in _HINTS.items() if k != "twitter"}  # no X fetches in these reads


async def _pool(db: str) -> asyncpg.Pool:
    from tokensage.db import create_pool

    return await create_pool(db, min_size=4, max_size=4)


# ------------------------------------------------- A1: no logo download for OCR when OCR is off


@needs_db
@pytest.mark.parametrize("enable_ocr", [False, True])
async def test_full_read_downloads_a_hashed_hinted_logo_only_for_ocr(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    enable_ocr: bool,
) -> None:
    """A hinted IPFS logo hashed by an earlier read was downloaded again by every full read,
    for an OCR pass that never runs with OCR off (the prod default)."""
    from tests.fixtures.chain import install_web

    c0 = await asyncpg.connect(migrated_db)
    try:
        await c0.execute("delete from image")  # not truncated between tests
    finally:
        await c0.close()
    ocr_calls: list[int] = []

    async def fake_ocr(data: bytes) -> tuple[list[ocr.OcrLine], str | None]:
        ocr_calls.append(len(data))
        return [], None

    monkeypatch.setattr(ocr, "read_async", fake_ocr)
    img_calls: list[str] = []

    def img(request: httpx.Request) -> httpx.Response:
        img_calls.append(str(request.url))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    router.get(url__regex=rf".*/ipfs/{CID_IMG}$").mock(side_effect=img)
    install_web(router, _chain())
    async with make_client(migrated_db, enable_ocr=enable_ocr) as c:
        basic = await c.post(
            f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 5}, json={"hints": HINTS}
        )
        assert basic.status_code == 200, basic.text
        n_basic = len(img_calls)
        md.image_memo.clear()  # the basic read's download is no longer fresh
        full = await c.post(
            f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 15}, json={"hints": HINTS}
        )
        assert full.status_code == 200, full.text
    assert n_basic >= 1
    a1 = TokenResponse.model_validate(basic.json()).analysis
    a2 = TokenResponse.model_validate(full.json()).analysis
    assert a1 is not None and a2 is not None and (a1.depth, a2.depth) == ("basic", "full")
    assert a2.image == a1.image and a2.image.status == "ok" and a2.image.phash
    if enable_ocr:
        assert len(img_calls) > n_basic and ocr_calls  # OCR still gets the bytes it needs
    else:
        assert len(img_calls) == n_basic and not ocr_calls


# ------------------------------------------------- A2: basic X tier B shares the in-flight fetch


@needs_db
async def test_basic_x_tier_b_shares_the_full_reads_fetch(
    migrated_db: str, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """basic_x_load called the inner fetch, so concurrent basic reads of a copycat wave (and
    the same mint's full read) each fetched the linked post from the mirrors."""
    calls = {"tweet": 0, "profile": 0}

    async def fake_fetch_tweet(http: Any, tweet_id: str, **_: Any) -> xs.TweetData:
        calls["tweet"] += 1
        await asyncio.sleep(0.2)
        return xs.TweetData(id=tweet_id, status="ok", source="fxtwitter", text="gm")

    async def fake_fetch_profile(http: Any, handle: str) -> xs.ProfileData:
        calls["profile"] += 1
        await asyncio.sleep(0.2)
        return xs.ProfileData(handle=handle, status="ok", source="fxtwitter", followers=5)

    monkeypatch.setattr(fulldepth, "fetch_tweet", fake_fetch_tweet)
    monkeypatch.setattr(fulldepth, "fetch_profile", fake_fetch_profile)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    post = analyzer._x_info("https://x.com/someone/status/4242", None)
    prof = analyzer._x_info("https://x.com/someone", None)
    assert post is not None and prof is not None
    pool = await _pool(migrated_db)
    try:

        async def basic(x: Any) -> fulldepth.BasicXLoad:
            async with pool.acquire() as c:
                return await fulldepth.basic_x_load(c, None, s, x)  # type: ignore[arg-type]

        async def full_tweet() -> xs.TweetData:
            async with pool.acquire() as c:
                return await fulldepth.tweet_cached(c, None, s, "4242")  # type: ignore[arg-type]

        got = await asyncio.gather(basic(post), basic(post), full_tweet())
        profs = await asyncio.gather(basic(prof), basic(prof))
    finally:
        await pool.close()
    assert calls == {"tweet": 1, "profile": 1}
    assert all(b.tweet is not None and b.tweet.text == "gm" for b in got[:2])
    assert all(not b.caveats for b in got[:2])
    assert all(b.profile is not None and b.profile.followers == 5 for b in profs)
    assert not fulldepth._inflight_locks


# ------------------------------------------------- A3: story heat, one GeckoTerminal search


@needs_db
async def test_concurrent_story_heats_search_once(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coins of one story refresh together; each missed the cached heat and spent its
    own search (and peak) from GeckoTerminal's 10-a-minute budget."""
    calls: list[str] = []

    async def fake_search(http: Any, query: str) -> list[geckoterminal.PoolQuote]:
        calls.append(query)
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(geckoterminal, "search_pools", fake_search)
    doc = {"referent": {"label": "Moo Deng Heat", "generic": False, "source": "entities:x"}}
    pool = await _pool(migrated_db)
    try:
        async with pool.acquire() as c:
            await c.execute("delete from lookup_cache where key like 'heat:%'")

        async def one() -> StoryHeat | None:
            async with pool.acquire() as c:
                return await now.story_heat(c, None, doc)  # type: ignore[arg-type]

        heats = await asyncio.gather(*(one() for _ in range(4)))
    finally:
        await pool.close()
    assert calls == ["Moo Deng Heat"]
    assert all(h is not None for h in heats)
    assert len({h.model_dump_json() for h in heats if h is not None}) == 1
    assert not fulldepth._inflight_locks


# ------------------------------------------------- A4: X media, one download per URL


@needs_db
async def test_concurrent_media_hashes_download_each_url_once(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokensage.resolve import metadata

    url = "https://pbs.twimg.com/media/audit1023.jpg"
    calls: list[str] = []

    async def fake_fetch_url(http: Any, u: str, settings: Settings, **_: Any) -> Fetched:
        calls.append(u)
        await asyncio.sleep(0.2)
        return Fetched(url=u, status=200, content_type="image/png", body=PNG)

    monkeypatch.setattr(metadata, "fetch_url", fake_fetch_url)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    pool = await _pool(migrated_db)
    try:
        async with pool.acquire() as c:
            await c.execute("delete from x_media where url=$1", url)

        async def one() -> list[Any]:
            async with pool.acquire() as c:
                return await fulldepth.media_hashes(c, None, s, [url])  # type: ignore[arg-type]

        got = await asyncio.gather(*(one() for _ in range(4)))
        async with pool.acquire() as c:
            row = await c.fetchrow("select status, phash from x_media where url=$1", url)
    finally:
        await pool.close()
    assert len(calls) == 1
    assert all(len(g) == 1 and g[0].status == "ok" and g[0].phash for g in got)
    assert len({(g[0].phash, g[0].phash_mirror) for g in got}) == 1
    assert row["status"] == "ok" and row["phash"] == got[0][0].phash
    assert not fulldepth._inflight_locks


# ------------------------------------------------- A5: a successful metadata retry fetches once


@needs_db
async def test_successful_metadata_retry_fetches_the_json_once(
    conn: asyncpg.Connection,  # noqa: F811
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_metadata fetched and stored the metadata, then analyze found the row without
    hashes and fetched the same JSON again just to hash the logo."""
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    ctx = analyzer.Context.create(_settings(migrated_db))
    chain = _chain()
    try:
        with respx.mock(assert_all_called=False) as r:
            _web(r, chain, meta_ok=False, img_ok=True)
            await analyzer.analyze(conn, ctx, T22_MINT, "basic")
        assert (
            await conn.fetchval("select status from token_metadata where mint=$1", T22_MINT)
        ) == "unresolved"
        md.image_memo.clear()
        with respx.mock(assert_all_called=False) as r:
            meta_calls, img_calls = _web(r, chain, meta_ok=True, img_ok=True)
            rv = await analyzer.retry_metadata(conn, ctx, T22_MINT, "basic", job_id=None)
        assert rv is not None
        assert len(meta_calls) == 1 and len(img_calls) == 1
        doc = await conn.fetchval(
            "select doc from analysis where mint=$1 and version=$2", T22_MINT, rv
        )
        assert doc["image"]["status"] == "ok" and doc["image"]["phash"]
        key = await conn.fetchval(
            "select image_content_key from token_metadata where mint=$1", T22_MINT
        )
        assert await conn.fetchval("select phash from image where content_key=$1", key)
    finally:
        await ctx.close()


# ------------------------------------------------- A6: breaker on pump.fun coins-v2

MINT = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
COINS_V2 = f"https://frontend-api-v3.pump.fun/coins-v2/{MINT}"


def _reopen_now() -> None:
    """The breaker's cooldown has run out (without sleeping through it)."""
    breaker.states[resolver.PUMP_COINS_SOURCE].open_until = time.monotonic() - 1


@respx.mock
async def test_coins_v2_refused_by_cloudflare_is_skipped_until_one_probe() -> None:
    async def slow_ok(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)  # the probe is still in flight when the others ask
        return httpx.Response(200, json={"created_timestamp": 1.7e12})

    route = respx.get(COINS_V2).mock(return_value=httpx.Response(403, text="Just a moment..."))
    async with httpx.AsyncClient() as http:
        assert await resolver._created_from_frontend_api(http, MINT) == (None, None)
        # open: the same answer a refused call gives, without the call
        assert await resolver._created_from_frontend_api(http, MINT) == (None, None)
        assert route.call_count == 1
        _reopen_now()
        route.mock(side_effect=slow_ok)
        got = await asyncio.gather(
            *(resolver._created_from_frontend_api(http, MINT) for _ in range(3))
        )
        assert route.call_count == 2  # one probe; the others did not wait on it
        assert got.count((None, None)) == 2 and got[0][0] is not None
        # the probe succeeded: the breaker is closed again
        assert breaker.allow(resolver.PUMP_COINS_SOURCE)
        assert (await resolver._created_from_frontend_api(http, MINT))[0] is not None
    assert route.call_count == 3


@respx.mock
async def test_coins_v2_breaker_counts_only_api_failures() -> None:
    src = resolver.PUMP_COINS_SOURCE
    route = respx.get(COINS_V2).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        for _ in range(breaker.threshold + 1):
            assert await resolver._created_from_frontend_api(http, MINT) == (None, None)
        assert route.call_count == breaker.threshold + 1  # a 404 is about the coin
        assert breaker.allow(src)
        route.mock(return_value=httpx.Response(503))
        for _ in range(breaker.threshold + 2):
            assert await resolver._created_from_frontend_api(http, MINT) == (None, None)
        assert route.call_count == 2 * breaker.threshold + 1  # open after `threshold` 5xx
        assert not breaker.allow(src)
        _reopen_now()
        route.mock(side_effect=httpx.ConnectTimeout("slow"))
        assert await resolver._created_from_frontend_api(http, MINT) == (None, None)
        assert not breaker.allow(src)  # a failed probe opens it again at once


def test_allow_probe_holds_the_others_until_the_probe_reports() -> None:
    b = CircuitBreaker(threshold=2, cooldown_s=60.0)
    assert b.allow_probe("s", 5.0) and b.allow_probe("s", 5.0)  # closed: everyone goes
    b.failure("s")
    assert b.allow_probe("s", 5.0)  # below the threshold: still closed
    assert b.failure("s") and not b.allow_probe("s", 5.0)
    b.states["s"].open_until = time.monotonic() - 1
    assert b.allow_probe("s", 5.0)
    assert not b.allow_probe("s", 5.0) and not b.allow("s")
    assert b.failure("s")  # the probe failed: open for the cooldown, not the hold
    assert b.states["s"].open_until > time.monotonic() + 30
    b.states["s"].open_until = time.monotonic() - 1
    assert b.allow_probe("s", 5.0)
    b.success("s")
    assert b.allow_probe("s", 5.0) and b.allow_probe("s", 5.0)
