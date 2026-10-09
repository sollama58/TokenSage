"""Audit 0.28, I/O group: untrusted metadata / on-chain strings, fetch hygiene, IPFS
verdicts, callback delivery, cache single-flight and the knowledge cron's robustness."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
import respx
import structlog

from tests.conftest import needs_db
from tests.fixtures.chain import (
    GW1,
    GW2,
    RPC,
    SPL_MINT,
    FakeChain,
    install_web,
    public_resolver,
)
from tokensage import callbacks, fulldepth
from tokensage.config import Settings
from tokensage.net import safe_fetch
from tokensage.net.ipfs import IpfsRef, fetch_ipfs
from tokensage.net.safe_fetch import FetchError, safe_get
from tokensage.resolve import metadata as md
from tokensage.resolve import metaplex, resolver
from tokensage.resolve.rpc import SolanaRpc
from tokensage.sources import coingecko, geckoterminal, lookups, wikimedia
from tokensage.sources import x as xs

CID = "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    await conn.execute(
        "truncate token_metadata, image, x_ref, token_market, x_tweet, x_profile, lookup_cache"
        " cascade"
    )
    try:
        yield conn
    finally:
        await conn.close()


def _settings(**over: Any) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


# ------------------------------------------------------------- RES-1 / TDD-2 / SEC-6 / EC-4


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1",  # unbalanced bracket: urlsplit raises "Invalid IPv6 URL"
        "https://[",
        "https://x.com／dogwifcap",  # fullwidth slash: NFKC netloc check raises
        "https://x.com＠evil",
        "https://a.b/x\r\nX: y",  # CR/LF: header injection for a consumer that logs it
        "https://a.b/x\x7fy",
        "https://evil@127.0.0.1/",  # userinfo: our own fetcher refuses it
        "https://a:b@c.d/",
    ],
)
def test_clean_url_never_raises_and_drops_hostile_urls(url: str) -> None:
    assert md.clean_url(url) is None
    assert md.clean_social(url) is None


def test_build_and_from_hints_survive_bracket_urls() -> None:
    m = md.build(
        "https://u.test/m",
        b'{"name":"x","image":"https://[::1","website":"https://[","twitter":"https://x.com\\uff0fh"}',
    )
    assert m.status == "ok" and m.name == "x"
    assert m.image_url is None and m.website is None and m.twitter is None
    h = md.from_hints({"website": "https://[abc", "image_url": "https://[::1", "name": "n"})
    assert h.status == "ok" and h.website is None and h.image_url is None and h.name == "n"


@respx.mock
async def test_fetch_metadata_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get("https://ex.test/m.json").mock(
        return_value=httpx.Response(200, json={"name": "x", "website": "https://[::1"})
    )
    async with httpx.AsyncClient() as c:
        m = await md.fetch_metadata(c, "https://ex.test/m.json", _settings())
    assert m.status == "ok" and m.website is None

    def boom(uri: str, body: bytes) -> md.Metadata:
        raise RuntimeError("hostile json")

    monkeypatch.setattr(md, "build", boom)
    async with httpx.AsyncClient() as c:
        m = await md.fetch_metadata(c, "https://ex.test/m.json", _settings())
    assert m.status == "invalid" and "hostile json" in (m.error or "")


def test_wrong_typed_fields_are_absent_not_reprs() -> None:
    m = md.build(
        "https://u.test/m",
        b'{"name":["a","b"],"symbol":{"x":1},"description":[1,2],"image":5,"twitter":12345}',
    )
    assert m.status == "ok"
    assert m.name is None and m.symbol is None and m.description is None
    assert m.image_url is None and m.twitter is None
    # numbers are still accepted as names (test_build_metadata_tolerates_junk: 123 -> "123")
    assert md.build("https://u.test/m", b'{"name": 123}').name == "123"


# ------------------------------------------------------------- RES-2


@needs_db
async def test_metadata_retry_schedule_starts_at_first_step(db: asyncpg.Connection) -> None:
    m = md.Metadata(status="unresolved", error="x")
    await db.execute("insert into token (mint) values ('m')")
    delays: list[float | None] = []
    for attempts in (1, 2, 6, 7):
        await db.execute("delete from token_metadata where mint='m'")
        await md.persist(db, "m", m, attempts)
        nxt = await db.fetchval("select next_retry_at from token_metadata where mint='m'")
        delays.append((nxt - datetime.now(UTC)).total_seconds() if nxt else None)
    assert delays[3] is None  # the 6th failure was the last scheduled retry
    assert delays[0] is not None and 20 < delays[0] <= 30  # first retry after 30 s
    assert delays[1] is not None and 110 < delays[1] <= 120
    assert delays[2] is not None and 3590 < delays[2] <= 3600


# ------------------------------------------------------------- RES-3


@respx.mock
@pytest.mark.parametrize("ts", [1e300, 2**62, 1e18])
async def test_frontend_api_timestamp_out_of_range_is_ignored(ts: float) -> None:
    mint = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    respx.get(f"https://frontend-api-v3.pump.fun/coins-v2/{mint}").mock(
        return_value=httpx.Response(200, json={"created_timestamp": ts, "creator": "abc"})
    )
    async with httpx.AsyncClient() as http:
        created, creator = await resolver._created_from_frontend_api(http, mint)
    assert created is None and creator == "abc"


# ------------------------------------------------------------- RES-4


class _NoDasChain(FakeChain):
    """A provider that answers getAsset with HTTP 200 and a JSON-RPC error (Helius on a
    missing asset)."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "getAsset":
            self.calls.append("getAsset")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32603, "message": "RecordNotFound Error: Asset Not Found"},
                },
            )
        return super().handle(request)


@respx.mock
async def test_pair_token_pumpfun_flag_survives_a_metadata_failure() -> None:
    from tests.fixtures.chain import SPL_TOKEN, acct, b64, curve_bytes, parsed_mint
    from tokensage.resolve.pump_ca import PUMP_PROGRAM, bonding_curve_pda

    chain = _NoDasChain()
    mint = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    chain.accounts[mint] = parsed_mint(SPL_TOKEN)  # no Metaplex PDA
    chain.accounts[bonding_curve_pda(mint)] = acct(PUMP_PROGRAM, b64(curve_bytes(10**14, False)))
    respx.post(RPC).mock(side_effect=chain.handle)
    async with httpx.AsyncClient() as http:
        meta, pumpfun = await resolver.read_pair_mint(SolanaRpc(RPC, http), mint)
    assert meta is None and pumpfun is True
    assert "getAsset" in chain.calls


# ------------------------------------------------------------- SEC-3


def test_borsh_string_drops_interior_nul() -> None:
    assert metaplex._borsh_string(b"\x07\x00\x00\x00Foo\x00Bar", 0) == ("FooBar", 11)
    d = metaplex.decode_metadata(metaplex.encode_metadata_for_tests("Foo\x00Bar", "X", "u"))
    assert d["name"] == "FooBar"


def test_onchain_metadata_is_sanitised_for_every_source() -> None:
    out = resolver._clean_onchain(
        {"name": "a\udc80b\x00c\n", "symbol": "S" * 100, "uri": "https://a/\x01b"}
    )
    assert out == {"name": "a?bc", "symbol": "S" * 64, "uri": "https://a/b"}
    assert resolver._clean_onchain(None) is None


@needs_db
async def test_resolve_persists_a_token_whose_onchain_name_has_a_nul(
    db: asyncpg.Connection,
) -> None:
    chain = FakeChain()
    chain.add_spl_pump(SPL_MINT, "Foo\x00Bar", "X\x00Y", f"https://ipfs.io/ipfs/{CID}")
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        async with httpx.AsyncClient() as http:
            res = await resolver.resolve(db, SolanaRpc(RPC, http), http, _settings(), SPL_MINT)
    assert res.name == "FooBar" and res.symbol == "XY"
    row = await db.fetchrow("select name, symbol from token where mint=$1", SPL_MINT)
    assert row and row["name"] == "FooBar" and row["symbol"] == "XY"


# ------------------------------------------------------------- EC-3


@respx.mock
async def test_ipfs_too_large_is_definitive_but_404_is_not() -> None:
    respx.get(f"{GW1}/ipfs/{CID}").mock(return_value=httpx.Response(200, content=b"x" * 1000))
    respx.get(f"{GW2}/ipfs/{CID}").mock(return_value=httpx.Response(200, content=b"x" * 1000))
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError, match="too large") as e:
            await fetch_ipfs(c, IpfsRef(CID), [GW1, GW2], max_bytes=100, timeout=5, stagger_s=0.01)
        assert e.value.retryable is False
        # the fetcher maps it to "invalid": no retry job for a document that cannot shrink
        m = await md.fetch_metadata(c, f"ipfs://{CID}", _settings(ipfs_gateways=f"{GW1},{GW2}"))
        assert m.status == "invalid"
    # a CID that has not propagated yet (404 everywhere) stays retryable
    respx.get(f"{GW1}/ipfs/{CID}").mock(return_value=httpx.Response(404))
    respx.get(f"{GW2}/ipfs/{CID}").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError) as e2:
            await fetch_ipfs(c, IpfsRef(CID), [GW1, GW2], max_bytes=100, timeout=5, stagger_s=0.01)
        assert e2.value.retryable is True


# ------------------------------------------------------------- SRC-1


@respx.mock
async def test_safe_get_refuses_a_compressed_answer() -> None:
    bomb = gzip.compress(b"\x00" * 300_000)  # a few hundred bytes that inflate to 300 KB
    respx.get("https://a.test/gz").mock(
        return_value=httpx.Response(
            200,
            content=bomb,
            headers={"content-encoding": "gzip", "content-length": str(len(bomb))},
        )
    )
    respx.get("https://a.test/plain").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-encoding": "identity"})
    )
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError, match="content-encoding") as e:
            await safe_get(c, "https://a.test/gz", max_bytes=65536, timeout=5)
        assert e.value.retryable is False
        f = await safe_get(c, "https://a.test/plain", max_bytes=65536, timeout=5)
        assert f.body == b"ok"


# ------------------------------------------------------------- SRC-2 / SRC-3 / CONC-2

TID = "1791351500217754008"


def _mock_free_chain(fx: httpx.Response, vx: httpx.Response, synd: httpx.Response) -> None:
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=fx)
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=vx)
    respx.get(url__startswith=xs.SYND).mock(return_value=synd)


@needs_db
@respx.mock
async def test_paid_deleted_verdict_is_final(db: asyncpg.Connection) -> None:
    _mock_free_chain(httpx.Response(404), httpx.Response(404), httpx.Response(404))
    respx.get(url__startswith=xs.OEMBED).mock(return_value=httpx.Response(404))
    paid = respx.get(url__startswith=xs.PAID).mock(
        return_value=httpx.Response(200, json={"status": "success", "tweets": []})
    )
    s = _settings(enable_paid_x=True, twitterapi_io_key="k")
    async with httpx.AsyncClient() as c:
        t = await fulldepth.tweet_cached(db, c, s, TID)
        assert t.status == "deleted" and t.source == "twitterapi_io" and paid.call_count == 1
        await db.execute("update x_tweet set fetched_at = now() - interval '2 hours'")
        t = await fulldepth.tweet_cached(db, c, s, TID)
    assert t.status == "deleted" and t.source == "twitterapi_io"
    assert paid.call_count == 1  # not asked again every hour
    assert respx.get(f"{xs.FX}/status/{TID}").call_count == 1


@needs_db
@respx.mock
async def test_oembed_record_is_not_refetched_on_every_call(db: asyncpg.Connection) -> None:
    _mock_free_chain(
        httpx.Response(200, json={"code": 500}),
        httpx.Response(200, json={}),
        httpx.Response(404),
    )
    oe = respx.get(url__startswith=xs.OEMBED).mock(
        return_value=httpx.Response(
            200, json={"html": "<p>hi</p>", "author_url": "https://twitter.com/jack"}
        )
    )
    synd = respx.get(url__startswith=xs.SYND)
    async with httpx.AsyncClient() as c:
        for _ in range(3):
            t = await fulldepth.tweet_cached(db, c, _settings(), TID)
            assert t.status == "ok" and t.source == "oembed"
    # one walk: the syndication CDN is asked once (not again by _supplement_links), and the
    # next two calls are served from the cache (upgrade retried after OEMBED_UPGRADE_RETRY)
    assert oe.call_count == 1 and synd.call_count == 1
    await db.execute("update x_tweet set fetched_at = now() - interval '45 minutes'")
    async with httpx.AsyncClient() as c:
        await fulldepth.tweet_cached(db, c, _settings(), TID)
    assert oe.call_count == 2  # the richer sources are tried again after the TTL


@needs_db
async def test_concurrent_lookups_fetch_once(
    db: asyncpg.Connection, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"tweet": 0, "news": 0}

    async def fake_fetch_tweet(http: Any, tweet_id: str, **_: Any) -> xs.TweetData:
        calls["tweet"] += 1
        await asyncio.sleep(0.2)
        return xs.TweetData(id=tweet_id, status="ok", source="fxtwitter", text="gm")

    async def fake_search(http: Any, term: str) -> list[Any]:
        calls["news"] += 1
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(fulldepth, "fetch_tweet", fake_fetch_tweet)
    monkeypatch.setattr(fulldepth.gnews, "search", fake_search)
    from tokensage.db import create_pool

    pool = await create_pool(migrated_db, min_size=8, max_size=8)
    try:

        async def one_tweet() -> xs.TweetData:
            async with pool.acquire() as conn:
                return await fulldepth.tweet_cached(conn, None, _settings(), "42")  # type: ignore[arg-type]

        async def one_news() -> Any:
            async with pool.acquire() as conn:
                return await fulldepth.news_lookup(conn, None, "Moo Deng", exact=True)  # type: ignore[arg-type]

        async with httpx.AsyncClient():
            tweets = await asyncio.gather(*(one_tweet() for _ in range(8)))
            news = await asyncio.gather(*(one_news() for _ in range(8)))
    finally:
        await pool.close()
    assert all(t.status == "ok" and t.text == "gm" for t in tweets)
    assert all(n is not None for n in news)
    assert calls == {"tweet": 1, "news": 1}
    assert not fulldepth._inflight_locks  # nothing leaks once the callers are done


# ------------------------------------------------------------- SRC-4


@respx.mock
async def test_search_lookups_never_raise_on_odd_json() -> None:
    respx.get(url__startswith=lookups.PUMP_SEARCH).mock(
        return_value=httpx.Response(200, json="Too many requests")
    )
    respx.get(url__startswith=lookups.DEX_SEARCH).mock(
        return_value=httpx.Response(200, json={"pairs": [None, 5]})
    )
    async with httpx.AsyncClient() as http:
        assert await lookups.pumpfun_search(http, "pepe") == []
        assert await lookups.dexscreener_search(http, "pepe") == []
        respx.get(url__startswith=lookups.PUMP_SEARCH).mock(
            return_value=httpx.Response(
                200, json={"coins": [{"mint": "M2", "name": 5, "symbol": ["PEPE"]}]}
            )
        )
        respx.get(url__startswith=lookups.DEX_SEARCH).mock(
            return_value=httpx.Response(200, json=[1, 2])
        )
        got = await lookups.pumpfun_search(http, "pepe")
        assert len(got) == 1 and got[0].name is None and got[0].symbol is None
        assert lookups.filter_same_name(got, "PEPE", "pepe") == []
        assert await lookups.dexscreener_search(http, "pepe") == []


# ------------------------------------------------------------- SRC-5


@respx.mock
async def test_knowledge_parsers_return_none_on_odd_bodies() -> None:
    respx.get(url__startswith=coingecko.BASE).mock(
        return_value=httpx.Response(200, json={"status": {"error_code": 10005}})
    )
    respx.get(url__startswith=geckoterminal.BASE).mock(return_value=httpx.Response(200, json=[1]))
    respx.get(url__startswith="https://wikimedia.org/").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "articles": [
                            {"article": "A", "views": "n/a"},
                            "x",
                            {"article": "B", "views": 3},
                        ]
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as http:
        assert await coingecko.category_markets(http, "", "pump-fun") is None
        assert await geckoterminal.top_pools(http, "pump-fun") is None
        assert await wikimedia.top_articles(http, datetime(2026, 1, 1).date()) == {"A": 0, "B": 3}


async def test_knowledge_cron_steps_are_isolated() -> None:
    from tokensage.jobs import knowledge

    ran: list[str] = []

    async def bad() -> dict[str, int]:
        raise AttributeError("'str' object has no attribute 'get'")

    async def good() -> dict[str, int]:
        ran.append("good")
        return {"rows": 1}

    assert await knowledge._step("knowledge.trends", bad()) is None
    assert await knowledge._step("knowledge.top_volume", good()) == {"rows": 1}
    assert ran == ["good"]


# ------------------------------------------------------------- SEC-2 / RT-5 / RT-6

HOOK = "https://consumer.example/hook?token=s3cret"
PAYLOAD = {"callback_url": HOOK, "key_digest": "00" * 32, "target_job_id": 1}


async def _drip() -> AsyncIterator[bytes]:
    while True:  # a few bytes every 0.6 s, forever: each read beats httpx's per-read timeout
        yield b"x"
        await asyncio.sleep(0.6)


async def test_callback_delivery_has_a_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(callbacks, "CALLBACK_TOTAL_TIMEOUT_S", 1.0)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=_Stream(_drip())))
    async with httpx.AsyncClient(transport=transport) as c:
        started = asyncio.get_running_loop().time()
        with pytest.raises(RuntimeError, match="timed out"):
            await callbacks.deliver(c, PAYLOAD, {"job_id": 1})
    assert asyncio.get_running_loop().time() - started < 5


class _Stream(httpx.AsyncByteStream):
    def __init__(self, gen: AsyncIterator[bytes]) -> None:
        self._gen = gen

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._gen:
            yield chunk


async def test_callback_delivery_reads_only_a_few_kb_and_logs_no_url() -> None:
    served = {"chunks": 0}

    async def huge() -> AsyncIterator[bytes]:
        for _ in range(40):
            served["chunks"] += 1
            yield b"y" * (1 << 20)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=_Stream(huge())))
    async with httpx.AsyncClient(transport=transport) as c:
        with structlog.testing.capture_logs() as logs:
            await callbacks.deliver(c, PAYLOAD, {"job_id": 1})
    assert served["chunks"] <= 2  # 42 MB offered, at most a couple of chunks read
    delivered = [e for e in logs if e["event"] == "callback.delivered"]
    assert delivered and delivered[0]["host"] == "consumer.example"
    assert "s3cret" not in json.dumps(delivered, default=str)
