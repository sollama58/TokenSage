"""X engagement on the read (since rules 0.30.0): counts, exact time and media of the linked
post and the post it answers, kept fresh for young coins; and the full-depth fetches run
concurrently under a job deadline. Offline: respx for the mirrors, a local Postgres.

The post is modelled on a real audited read (read-audit sample0: $CUMPOUND, launched seconds
after @elonmusk answered @MichaelDell with "Compound growth is the most powerful force in
the Universe")."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import T22_MINT, FakeChain, install_web, public_resolver
from tokensage import analyzer, fulldepth
from tokensage.api import service
from tokensage.api.schemas import TokenResponse
from tokensage.config import Settings
from tokensage.db import _init_connection
from tokensage.net import safe_fetch
from tokensage.sources import x as xs

POST_ID = "1977000000000000001"
PARENT_ID = "1976999000000000001"
VIDEO = "https://video.twimg.com/ext_tw_video/1/pu/vid/720x720/clip.mp4"
THUMB = "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/thumb.jpg"


def _post(
    likes: int, views: int, text: str = "Compound growth is the most powerful force in the Universe"
) -> dict[str, Any]:
    return {
        "code": 200,
        "status": {
            "id": POST_ID,
            "text": f"@MichaelDell {text}",
            "created_timestamp": 1760239000,
            "author": {
                "id": "44196397",
                "screen_name": "elonmusk",
                "name": "Elon Musk",
                "followers": 227000000,
                "verification": {"verified": True},
            },
            "replying_to": {"screen_name": "MichaelDell", "post": PARENT_ID},
            "likes": likes,
            "reposts": likes // 10,
            "replies": likes // 20,
            "quotes": 12,
            "bookmarks": 40,
            "views": views,
            "media": {
                "all": [
                    {"type": "video", "url": VIDEO, "thumbnail_url": THUMB},
                    {"type": "photo", "url": "javascript:alert(1)"},
                ]
            },
        },
    }


PARENT = {
    "code": 200,
    "status": {
        "id": PARENT_ID,
        "text": "Compound growth. Every day, a little better.",
        "created_timestamp": 1760238000,
        "author": {"id": "1", "screen_name": "MichaelDell", "name": "Michael Dell"},
        "likes": 900,
        "reposts": 80,
        "replies": 60,
        "views": 250000,
        "media": {"photos": [{"url": "https://pbs.twimg.com/media/dell.jpg"}]},
    },
}


# ----------------------------------------------------------------- parsing (no database)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


async def test_fx_engagement_and_media_are_parsed(http: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{xs.FX}/status/{POST_ID}").mock(
            return_value=httpx.Response(200, json=_post(41000, 9_800_000))
        )
        t = await xs.fx_tweet(http, POST_ID)
    assert t is not None and t.status == "ok"
    assert (t.likes, t.reposts, t.replies, t.quotes, t.bookmarks, t.views) == (
        41000,
        4100,
        2050,
        12,
        40,
        9_800_000,
    )
    # the javascript: "photo" is dropped by the URL sanitiser; the video keeps its file
    assert t.media == [{"type": "video", "url": VIDEO, "thumbnail_url": THUMB}]
    back = xs.TweetData.from_json(t.to_json())
    assert back.media == t.media and back.quotes == 12 and back.fetched_at is None


def test_syndication_media_types() -> None:
    j = {
        "mediaDetails": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg"},
            {
                "type": "animated_gif",
                "media_url_https": "https://pbs.twimg.com/tweet_video_thumb/b.jpg",
                "video_info": {
                    "variants": [
                        {"content_type": "video/mp4", "url": "https://video.twimg.com/b.mp4"}
                    ]
                },
            },
            {"type": "video", "media_url_https": "data:image/png;base64,xx"},
        ]
    }
    assert xs._synd_media_items(j) == [
        {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "thumbnail_url": None},
        {
            "type": "gif",
            "url": "https://video.twimg.com/b.mp4",
            "thumbnail_url": "https://pbs.twimg.com/tweet_video_thumb/b.jpg",
        },
    ]


# ----------------------------------------------------------------- service cache rule


def _doc(analyzed_ago_s: float, as_of_ago_s: float | None) -> dict[str, Any]:
    now = datetime.now(UTC)
    as_of = (now - timedelta(seconds=as_of_ago_s)).isoformat() if as_of_ago_s is not None else None
    return {
        "analyzed_at": (now - timedelta(seconds=analyzed_ago_s)).isoformat(),
        "x": {"engagement": {"likes": 1, "as_of": as_of}},
    }


def test_cached_read_of_a_young_coin_expires_with_its_engagement() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    young = datetime.now(UTC) - timedelta(minutes=30)
    old = datetime.now(UTC) - timedelta(hours=3)
    # counts 6 min old on a 30-min-old coin: re-run the read
    assert service.engagement_expired(_doc(200, 360), young, s)
    # counts within X_ENGAGEMENT_REFRESH_S: serve it
    assert not service.engagement_expired(_doc(200, 250), young, s)
    # older coins keep their usual max_age
    assert not service.engagement_expired(_doc(200, 3600), old, s)
    assert not service.engagement_expired(_doc(200, 3600), None, s)
    # a read under X_ENGAGEMENT_RETRY_S old is served even with old counts (re-fetch failed)
    assert not service.engagement_expired(_doc(30, 3600), young, s)
    # counts of unknown age (copy cached before rules 0.30.0) count as old
    assert service.engagement_expired(_doc(200, None), young, s)
    # a read without engagement (basic depth, no post) is not affected
    assert not service.engagement_expired({"analyzed_at": _doc(200, 0)["analyzed_at"]}, young, s)


def test_young_coin_age_comes_from_config() -> None:
    s = Settings(_env_file=None, x_engagement_young_s=600)  # type: ignore[call-arg]
    assert analyzer.engagement_young(datetime.now(UTC) - timedelta(minutes=5), s)
    assert not analyzer.engagement_young(datetime.now(UTC) - timedelta(minutes=15), s)
    assert not analyzer.engagement_young(None, s)


def test_doc_errors_name_the_timed_out_sources() -> None:
    errs = service.doc_errors({"timed_out": ["news", "x"]})
    assert [(e.source, e.code) for e in errs] == [("news", "timeout"), ("x", "timeout")]
    assert service.doc_errors({}) == []


# ----------------------------------------------------------------- deadline runner


async def test_until_deadline_runs_concurrently_and_names_the_late() -> None:
    steps = analyzer._Steps()
    done: list[str] = []

    async def step(name: str, s: float) -> None:
        async with steps.run(name):
            await asyncio.sleep(s)
        done.append(name)

    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    late = await analyzer._until_deadline(
        [step("a", 0.2), step("b", 0.2), step("c", 0.2)], steps, loop.time() + 5
    )
    assert late == [] and sorted(done) == ["a", "b", "c"]
    assert time.monotonic() - t0 < 0.5  # together, not one after the other

    done.clear()
    late = await analyzer._until_deadline(
        [step("fast", 0.05), step("slow", 5), step("slower", 5)], steps, loop.time() + 0.3
    )
    assert late == ["slow", "slower"] and done == ["fast"]
    assert steps.running == {}


async def test_until_deadline_raises_the_first_error() -> None:
    steps = analyzer._Steps()
    cancelled: list[int] = []

    async def boom() -> None:
        raise RuntimeError("mirror exploded")

    async def long() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    loop = asyncio.get_running_loop()
    with pytest.raises(RuntimeError, match="mirror exploded"):
        await analyzer._until_deadline([boom(), long()], steps, loop.time() + 5)
    assert cancelled == [1]


# ----------------------------------------------------------------- with a database


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)  # json codecs, as the worker pool has
    await conn.execute(
        "truncate token_metadata, image, x_ref, x_tweet, x_profile, x_profile_history, "
        "trend_term, lookup_cache, token_market, x_media cascade"
    )
    fulldepth._trend_cache = None
    try:
        yield conn
    finally:
        await conn.close()


async def _age_snapshot(db: asyncpg.Connection, tweet_id: str, seconds: int) -> None:
    """Pretend the stored copy (and the last attempt) is `seconds` old."""
    at = datetime.now(UTC) - timedelta(seconds=seconds)
    await db.execute(
        """update x_tweet set fetched_at=$2,
             latest = jsonb_set(latest, '{fetched_at}', to_jsonb($3::text))
           where tweet_id=$1""",
        tweet_id,
        at,
        at.isoformat(),
    )


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@needs_db
async def test_engagement_comes_from_the_newest_copy_text_from_the_first(
    db: asyncpg.Connection, http: httpx.AsyncClient
) -> None:
    s = _settings()
    calls: list[int] = []
    payload = {"p": _post(100, 5000)}

    def fx(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=payload["p"])

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{xs.FX}/status/{POST_ID}").mock(side_effect=fx)
        first = await fulldepth.tweet_cached(db, http, s, POST_ID, fresh_engagement=True)
        assert first.likes == 100 and first.fetched_at is not None
        # within X_ENGAGEMENT_REFRESH_S: served from cache
        again = await fulldepth.tweet_cached(db, http, s, POST_ID, fresh_engagement=True)
        assert len(calls) == 1 and again.likes == 100

        # six minutes later the counts have grown (and the mirror shows an edited text)
        await _age_snapshot(db, POST_ID, 360)
        payload["p"] = _post(41000, 9_800_000, text="edited later")
        fresh = await fulldepth.tweet_cached(db, http, s, POST_ID, fresh_engagement=True)
    assert len(calls) == 2
    assert fresh.likes == 41000 and fresh.views == 9_800_000 and fresh.quotes == 12
    assert fresh.text is not None and "most powerful force" in fresh.text  # first copy's text
    assert fresh.fetched_at is not None
    assert (datetime.now(UTC) - fresh.fetched_at).total_seconds() < 30
    row = await db.fetchrow("select first_snapshot, latest from x_tweet where tweet_id=$1", POST_ID)
    assert row["first_snapshot"]["likes"] == 100 and row["latest"]["likes"] == 41000

    # a later cache hit serves the newest counts with the first text, no fetch
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{xs.FX}/status/{POST_ID}").mock(side_effect=fx)
        cached = await fulldepth.tweet_cached(db, http, s, POST_ID)
    assert not route.called
    assert cached.likes == 41000 and "most powerful force" in (cached.text or "")


@needs_db
async def test_no_engagement_refresh_for_old_coins_or_right_after_a_failed_try(
    db: asyncpg.Connection, http: httpx.AsyncClient
) -> None:
    s = _settings()
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{xs.FX}/status/{POST_ID}").mock(
            return_value=httpx.Response(200, json=_post(100, 5000))
        )
        await fulldepth.tweet_cached(db, http, s, POST_ID)
    await _age_snapshot(db, POST_ID, 3600)

    # an old coin (fresh_engagement=False) keeps its cached copy
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{xs.FX}/status/{POST_ID}").mock(
            return_value=httpx.Response(200, json=_post(999, 1))
        )
        t = await fulldepth.tweet_cached(db, http, s, POST_ID, fresh_engagement=False)
    assert not route.called and t.likes == 100

    # the newest copy is old but the last attempt (a failure) was 10 s ago: no new try yet
    await db.execute(
        "update x_tweet set fetched_at = now() - interval '10 seconds' where tweet_id=$1", POST_ID
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{xs.FX}/status/{POST_ID}").mock(
            return_value=httpx.Response(200, json=_post(999, 1))
        )
        t = await fulldepth.tweet_cached(db, http, s, POST_ID, fresh_engagement=True)
    assert not route.called and t.likes == 100


@needs_db
async def test_serial_connection_shares_one_connection_between_tasks(
    db: asyncpg.Connection,
) -> None:
    sconn = fulldepth.serial(db)
    got = await asyncio.gather(*(sconn.fetchval("select $1::int", i) for i in range(20)))
    assert got == list(range(20))
    assert fulldepth.serial(sconn) is sconn


# ----------------------------------------------------------------- end to end


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


def _hints(created: datetime) -> dict[str, Any]:
    return {
        "name": "CUMPOUND GROWTH",
        "symbol": "CUMPOUND",
        "description": "the most powerful force in the universe",
        "twitter": f"https://x.com/elonmusk/status/{POST_ID}",
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


def _install_x(
    router: respx.MockRouter, post: dict[str, Any], calls: list[int], delay_s: float = 0
) -> None:
    async def fx(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if delay_s:
            await asyncio.sleep(delay_s)
        return httpx.Response(200, json=post)

    router.get(f"{xs.FX}/status/{POST_ID}").mock(side_effect=fx)
    router.get(f"{xs.FX}/status/{PARENT_ID}").mock(return_value=httpx.Response(200, json=PARENT))
    router.get(url__startswith=f"{xs.FX}/profile/").mock(return_value=httpx.Response(404))
    router.get(url__startswith=xs.VX).mock(return_value=httpx.Response(404))
    router.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text="<rss><channel></channel></rss>")
    )


async def _read(client: httpx.AsyncClient, hints: dict[str, Any], refresh: bool) -> TokenResponse:
    r = await client.post(
        f"/v1/tokens/{T22_MINT}",
        params={"depth": "full", "wait": 10, "refresh": str(refresh).lower()},
        json={"hints": hints},
    )
    assert r.status_code == 200, r.text
    return TokenResponse.model_validate(r.json())


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "CUMPOUND GROWTH", "CUMPOUND", "https://ipfs.io/ipfs/x", progress=0.3)
    return c


@needs_db
async def test_read_exposes_engagement_time_and_media_and_refreshes_young_coins(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    hints = _hints(datetime.now(UTC) - timedelta(minutes=20))
    calls: list[int] = []
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, _post(100, 5000), calls)
        install_web(router, _chain())
        body = await _read(client, hints, refresh=False)
    a = body.analysis
    assert body.status == "complete" and body.errors == []
    assert a is not None and a.x is not None and a.x.status == "ok"
    assert a.timed_out == []
    e = a.x.engagement
    assert e is not None and (e.likes, e.reposts, e.replies, e.quotes, e.bookmarks, e.views) == (
        100,
        10,
        5,
        12,
        40,
        5000,
    )
    assert e.as_of is not None and (datetime.now(UTC) - e.as_of).total_seconds() < 60
    assert a.x.created_at == datetime.fromtimestamp(1760239000, tz=UTC)
    assert [(m.type, m.url, m.thumbnail_url) for m in a.x.media] == [("video", VIDEO, THUMB)]
    rt = a.x.replied_to
    assert rt is not None and rt.id == PARENT_ID and rt.engagement is not None
    assert rt.engagement.likes == 900 and rt.engagement.views == 250000
    assert rt.engagement.quotes is None  # the mirror did not report it
    assert [m.url for m in rt.media] == ["https://pbs.twimg.com/media/dell.jpg"]
    assert len(calls) == 1

    # six minutes on: the stored copy is past X_ENGAGEMENT_REFRESH_S, the read re-fetches it
    await _age_snapshot(db, POST_ID, 360)
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, _post(41000, 9_800_000, text="edited"), calls)
        install_web(router, _chain())
        body = await _read(client, hints, refresh=True)
    a = body.analysis
    assert a is not None and a.x is not None and a.x.engagement is not None
    assert len(calls) == 2
    assert a.x.engagement.likes == 41000 and a.x.engagement.views == 9_800_000
    assert a.x.text is not None and "most powerful force" in a.x.text  # the first copy's text
    assert a.x.engagement.as_of is not None
    assert (datetime.now(UTC) - a.x.engagement.as_of).total_seconds() < 60


@needs_db
async def test_old_coin_keeps_its_cached_post(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    hints = _hints(datetime.now(UTC) - timedelta(days=3))
    calls: list[int] = []
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, _post(100, 5000), calls)
        install_web(router, _chain())
        await _read(client, hints, refresh=False)
    await _age_snapshot(db, POST_ID, 3600)
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, _post(41000, 9_800_000), calls)
        install_web(router, _chain())
        body = await _read(client, hints, refresh=True)
    a = body.analysis
    assert len(calls) == 1
    assert a is not None and a.x is not None and a.x.engagement is not None
    assert a.x.engagement.likes == 100
    assert a.x.engagement.as_of is not None
    assert (datetime.now(UTC) - a.x.engagement.as_of).total_seconds() >= 3600


@needs_db
async def test_deadline_returns_a_partial_read_naming_the_late_source(
    client: httpx.AsyncClient, settings: Settings, db: asyncpg.Connection
) -> None:
    settings.full_depth_deadline_s = 0.5
    hints = _hints(datetime.now(UTC) - timedelta(minutes=20))
    calls: list[int] = []
    t0 = time.monotonic()
    with respx.mock(assert_all_called=False) as router:
        _install_x(router, _post(100, 5000), calls, delay_s=5)
        install_web(router, _chain())
        body = await _read(client, hints, refresh=False)
    assert time.monotonic() - t0 < 4.5  # did not wait for the slow mirror
    a = body.analysis
    assert body.status == "partial" and a is not None
    assert "x" in a.timed_out
    assert ("x", "timeout") in {(e.source, e.code) for e in body.errors}
    assert any(c.startswith("partial: full-depth deadline") for c in a.caveats)
    # the post was never fetched: no engagement, and the read says the X lookup failed
    assert a.x is not None and a.x.engagement is None and a.x.status == "failed"
    # the rest of the read is there
    assert a.raw.name == "CUMPOUND GROWTH" and a.summary
    # a cache hit reports the same errors
    r = await client.post(
        f"/v1/tokens/{T22_MINT}", params={"depth": "full", "wait": 0}, json={"hints": hints}
    )
    cached = TokenResponse.model_validate(r.json())
    if cached.analysis is not None and cached.freshness.from_cache:
        assert ("x", "timeout") in {(e.source, e.code) for e in cached.errors}
