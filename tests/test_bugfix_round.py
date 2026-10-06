"""Regression tests for the audit round: each reproduces a reported bug."""

from __future__ import annotations

import asyncio
import io
import os
import time
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import numpy as np
import pytest
import respx
from PIL import Image

from tests.conftest import needs_db
from tokensage.engine import image as image_stage
from tokensage.engine import xsignals
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize, ticker_base
from tokensage.engine.pipeline import EngineInput, run_basic
from tokensage.engine.xmatch import match_name
from tokensage.engine.xref import parse_x_ref
from tokensage.net import safe_fetch
from tokensage.net.breaker import CircuitBreaker
from tokensage.resolve import metadata as md
from tokensage.sources import x as xs

MINT = "So11111111111111111111111111111111111111112"
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _run(name: str, symbol: str):  # type: ignore[no-untyped-def]
    return run_basic(EngineInput(MINT, name, symbol, None, None, T0))


# ----------------------------------------------------------------- fetch / metadata


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:99999/x",  # port out of range: was a bare ValueError
        "https://" + "a" * 70 + ".com/x",  # IDNA label too long: was a UnicodeError
        "https://100.100.100.200/latest/meta-data",  # CGNAT (Alibaba metadata): was allowed
    ],
)
async def test_bad_urls_are_unsafe_not_crashes(url: str) -> None:
    with pytest.raises(safe_fetch.UnsafeUrl):
        await safe_fetch.check_url(url)


async def test_fetch_has_an_overall_time_limit() -> None:
    async def trickle(request: httpx.Request) -> httpx.Response:
        async def body():  # type: ignore[no-untyped-def]
            for _ in range(100):
                await asyncio.sleep(0.2)  # each read is fast enough, the whole is not
                yield b"x"

        return httpx.Response(200, content=body())

    with respx.mock() as router:
        router.get("https://slow.test/a").mock(side_effect=trickle)
        async with httpx.AsyncClient() as c:
            t0 = time.perf_counter()
            with pytest.raises(safe_fetch.FetchError, match="timeout"):
                await safe_fetch.safe_get(
                    c,
                    "https://slow.test/a",
                    max_bytes=10_000,
                    timeout=1.0,
                    resolver=_public,
                )
            assert time.perf_counter() - t0 < 3


async def _public(host: str, port: int, **_: object) -> list:  # type: ignore[type-arg]
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def test_hostile_metadata_degrades_instead_of_crashing() -> None:
    assert md.build("https://x/a", b"[" * 60_000).status == "invalid"  # was RecursionError
    m = md.build(
        "https://x/a", b'{"name":"x\\u0000y","description":"d\\ud800","o":{"k":"\\u0000"}}'
    )
    assert m.status == "ok" and m.name == "xy"
    assert m.description is not None
    m.description.encode("utf-8")  # a lone surrogate used to make the DB write fail
    assert "\x00" not in str(m.raw)


def test_breaker_does_not_grow_with_healthy_hosts() -> None:
    b = CircuitBreaker()
    for i in range(5000):
        b.success(f"host:{i}.example")
    assert b.states == {}


# ----------------------------------------------------------------- engine


@pytest.mark.parametrize("symbol", ["BONK", "BRETT", "BOME", "FARTCOIN", "SHIB"])
def test_famous_coins_are_not_copycats_of_themselves(symbol: str) -> None:
    coin = next(c for c in load_knowledge().coins if c.symbol == symbol)
    out = _run(coin.name, coin.symbol)
    assert "copycat" not in {f.code for f in out.flags}
    assert symbol not in {c.get("ticker") for c in out.copy_of}


@pytest.mark.parametrize(("ticker", "base"), [("BBONK", "BONK"), ("BRETT2", "BRETT")])
def test_affixed_copycats_resolve_to_the_real_ticker(ticker: str, base: str) -> None:
    assert ticker_base(ticker, load_knowledge())[0] == base


def test_long_names_do_not_stall_the_worker() -> None:
    _run("warm", "W")
    t0 = time.perf_counter()
    _run("q" * 20_000, "X")  # used to take ~40 s in segmentation
    _run(" ".join(["abcdefg"] * 3000), "X")
    assert time.perf_counter() - t0 < 2


@pytest.mark.parametrize(
    ("name", "post"), [("Cat", "Big news on education reform"), ("Dog", "Best hotdog stand")]
)
def test_name_match_is_word_bounded(name: str, post: str) -> None:
    assert match_name(normalize(name, None, None), post).how == "none"


def _png16(arr: np.ndarray) -> bytes:
    b = io.BytesIO()
    Image.fromarray(arr.astype(np.uint16)).save(b, format="PNG")
    return b.getvalue()


def test_16bit_images_hash_by_content() -> None:
    rng = np.random.default_rng(1)
    a = np.kron(rng.integers(0, 65535, (8, 8)), np.ones((32, 32)))
    c = np.kron(rng.integers(0, 65535, (8, 8)), np.ones((32, 32)))
    fa, fc = image_stage.features(_png16(a)), image_stage.features(_png16(c))
    assert image_stage.hamming(fa.phash, fc.phash) > 14  # used to be 0 (all near-white)


def test_naive_datetimes_do_not_crash_x_signals() -> None:
    t = xs.TweetData(
        id="1",
        status="ok",
        text="hi",
        author_handle="a",
        followers=1,
        created_at=datetime(2026, 9, 30),
    )  # naive, as from an old cache row
    assert xsignals.assess("tweet", "a", t, None, T0, "X", MINT, []).relation


# ----------------------------------------------------------------- X sources


@pytest.fixture(autouse=True)
def _fresh_breaker():  # type: ignore[no-untyped-def]
    """Some tests here make every mirror fail; don't leave the shared breaker open."""
    from tokensage.net.breaker import breaker

    breaker.states.clear()
    yield
    breaker.states.clear()


def test_bad_timestamps_are_none_and_z_is_utc() -> None:
    assert xs._dt(1e20) is None and xs._dt(float("nan")) is None
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        assert xs._dt("2024-01-01T00:00:00Z") == datetime(2024, 1, 1, tzinfo=UTC)
    finally:
        if old is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = old
        time.tzset()


@respx.mock
async def test_paid_fallback_errors_never_fail_or_fake_a_deletion() -> None:
    for fx in ("https://api.fxtwitter.com", "https://api.vxtwitter.com"):
        respx.get(url__startswith=fx).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=xs.SYND).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=xs.OEMBED).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as c:
        for body in (
            [1, 2],
            {"tweets": {"a": 1}},
            {"tweets": ["x"]},
            {"status": "error", "msg": "Credits is not enough"},
        ):
            respx.get(url__startswith=xs.PAID).mock(return_value=httpx.Response(200, json=body))
            t = await xs.fetch_tweet(c, "123", paid_key="k", allow_paid=True)
            assert t.status == "failed", body  # not an exception, not "deleted"


def test_malformed_tweet_ids_are_rejected() -> None:
    for u in ("https://x.com/foo/status/1234567890123456789%0A", "https://x.com/foo/status/١٢٣"):
        assert parse_x_ref(u).get("tweet_id") is None


# ----------------------------------------------------------------- DB-backed


@pytest.fixture
async def db(migrated_db: str, clean_tables: None):  # type: ignore[no-untyped-def]
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    await conn.execute("truncate token_metadata, trend_term, x_tweet cascade")
    try:
        yield conn
    finally:
        await conn.close()


@needs_db
async def test_metadata_retry_chain_continues(db: asyncpg.Connection) -> None:
    from tokensage import analyzer, queue

    await db.execute("insert into token (mint) values ($1)", MINT)
    await db.execute(
        """insert into token_metadata (mint, status, attempts, next_retry_at)
           values ($1, 'unresolved', 2, now() + interval '10 minutes')""",
        MINT,
    )
    await db.execute(
        """insert into job (kind, mint, depth, status)
           values ('retry_metadata', $1, 'basic', 'pending')""",
        MINT,
    )
    job = await queue.claim(db, lease_s=60)
    assert job is not None and job.status == "running"
    # used to insert a new row, collide with this running job, and silently do nothing
    assert await analyzer._schedule_retry(db, MINT, "basic", own_job_id=job.id)
    row = await db.fetchrow(
        "select status, run_after > now() as later from job where id=$1", job.id
    )
    assert row["status"] == "pending" and row["later"]


@needs_db
async def test_concurrent_requests_cannot_overspend_the_quota(
    migrated_db: str, clean_tables: None
) -> None:
    from tests.fixtures.chain import MISSING, SPL_MINT, T22_MINT
    from tests.test_phase5_integration import make_client

    async with make_client(migrated_db, inline_analyzer=False, full_per_day_default=1) as c:
        rs = await asyncio.gather(
            *[
                c.get(f"/v1/tokens/{m}", params={"depth": "full", "wait": 0})
                for m in (T22_MINT, SPL_MINT, MISSING)
            ]
        )
    assert sorted(r.status_code for r in rs) == [202, 429, 429]
    conn = await asyncpg.connect(migrated_db)
    try:
        assert await conn.fetchval("select full_calls from api_usage") == 1
        assert await conn.fetchval("select count(*) from job") == 1
    finally:
        await conn.close()


@needs_db
async def test_retry_does_not_wake_waiters_and_finished_jobs_stay_finished(
    migrated_db: str, db: asyncpg.Connection
) -> None:
    from tokensage import queue
    from tokensage.db import create_pool

    pool = await create_pool(migrated_db, min_size=1, max_size=3)
    waiter = queue.DoneWaiter(pool)
    await waiter.start()
    try:
        j = await queue.enqueue(db, "analyze", MINT, "full")
        await queue.claim(db, lease_s=60)
        task = asyncio.create_task(waiter.wait(j.id, timeout_s=1.0))
        await asyncio.sleep(0.1)
        await queue.fail(db, j.id, "transient", max_attempts=3)  # back to pending
        assert await task is False  # used to wake the request at once with a 202
        # a late failure must not flip a finished job back to pending
        await db.execute("update job set run_after = now() where id=$1", j.id)
        assert (await queue.claim(db, lease_s=60)) is not None
        await queue.complete(db, j.id, 1)
        await queue.fail(db, j.id, "late", max_attempts=3)
        assert (await queue.get(db, j.id)).status == "done"  # type: ignore[union-attr]
    finally:
        await waiter.stop()
        await pool.close()


@needs_db
async def test_concurrent_analysis_stores_get_distinct_versions(
    migrated_db: str, db: asyncpg.Connection
) -> None:
    from tokensage.analyzer import _store_analysis
    from tokensage.api.schemas import Analysis, Versions
    from tokensage.db import create_pool

    await db.execute("insert into token (mint) values ($1)", MINT)
    pool = await create_pool(migrated_db, min_size=4, max_size=4)
    try:

        async def store() -> int:
            async with pool.acquire() as c:
                doc = Analysis(
                    mint=MINT,
                    summary="s",
                    depth="basic",
                    analyzed_at=T0,
                    versions=Versions(rules="r", lexicon="l"),
                )
                return await _store_analysis(c, doc)

        versions = await asyncio.gather(*[store() for _ in range(4)])
    finally:
        await pool.close()
    assert sorted(versions) == [1, 2, 3, 4]  # used to raise UniqueViolation


@needs_db
async def test_trend_index_keeps_each_terms_strongest_day(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth

    today = date.today()
    for d, spike in ((today, 9.0), (today - timedelta(days=2), 1.0)):
        await db.execute(
            """insert into trend_term (term, source, score, spike, first_seen, day, views)
               values ('Peanut (squirrel)', 'wikipedia', 1, $1, $2, $2, 200000)""",
            spike,
            d,
        )
    fulldepth._trend_cache = None
    idx = await fulldepth.trend_index(db)
    hits = idx.match("peanut the squirrel", "name")
    assert hits and hits[0].term.spike == 9.0


@needs_db
async def test_paid_x_daily_cap_is_enforced(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth
    from tokensage.api import usage
    from tokensage.config import Settings

    s = Settings(
        enable_paid_x=True,
        twitterapi_io_key="k",
        paid_x_daily_usd_cap=0.001,
        paid_x_usd_per_call=0.0005,
        _env_file=None,
    )  # type: ignore[call-arg]
    assert await fulldepth._paid_x_allowed(db, s)
    await usage.bump(db, fulldepth.PAID_X_USAGE_KEY, requests=2)
    assert not await fulldepth._paid_x_allowed(db, s)


@needs_db
@respx.mock
async def test_failed_recheck_keeps_a_good_tweet(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth
    from tokensage.config import Settings

    good = xs.TweetData(id="77", status="ok", source="fxtwitter", text="gm")
    await db.execute(
        """insert into x_tweet (tweet_id, first_snapshot, latest, status, source, fetched_at)
           values ('77', $1, $1, 'ok', 'fxtwitter', now() - interval '1 day')""",
        good.to_json(),
    )
    respx.route().mock(return_value=httpx.Response(500))  # every mirror down
    async with httpx.AsyncClient() as c:
        t = await fulldepth.tweet_cached(db, c, Settings(_env_file=None), "77")  # type: ignore[call-arg]
    assert t.status == "ok" and t.text == "gm"
    assert await db.fetchval("select status from x_tweet where tweet_id='77'") == "ok"
