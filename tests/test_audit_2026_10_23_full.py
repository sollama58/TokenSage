"""Regression tests for the full audit of 2026-10-23: SD-1 (header precheck before Pillow
decodes a hostile logo) and TS-1 to TS-4."""

from __future__ import annotations

import asyncio
import io
import struct
import time
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from PIL import Image

from tests.conftest import needs_db
from tests.fixtures.chain import PNG
from tokensage import analyzer, fulldepth, now
from tokensage.api.schemas import StoryHeat
from tokensage.config import Settings
from tokensage.engine import image as image_stage
from tokensage.engine import ocr, vision
from tokensage.net.breaker import breaker
from tokensage.net.safe_fetch import FetchError
from tokensage.sources import geckoterminal, lookups
from tokensage.sources import x as xs
from tokensage.sources.lookups import SameNameToken

# ------------------------------------------------------------------ SD-1: header precheck


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _apng_bomb(w: int = 16000, h: int = 16000) -> bytes:
    """The GHSA-5xm2-6676-qcp9 shape: a tiny APNG whose first frame is disposed to the
    background at its IHDR size."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    actl = struct.pack(">II", 1, 0)
    fctl = struct.pack(">IIIIIHHBB", 0, w, h, 0, 0, 1, 1, 1, 0)  # dispose_op=BACKGROUND
    idat = zlib.compress(b"\x00" * 16)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"acTL", actl)
        + _chunk(b"fcTL", fctl)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _gif(frames: int = 1, size: tuple[int, int] = (32, 32)) -> bytes:
    ims = [Image.new("RGB", size, (i * 40 % 256, 0, 0)) for i in range(frames)]
    buf = io.BytesIO()
    ims[0].save(buf, format="GIF", save_all=frames > 1, append_images=ims[1:], duration=50)
    return buf.getvalue()


def _gif_header_len(data: bytes) -> int:
    flags = data[10]
    return 13 + ((3 << ((flags & 7) + 1)) if flags & 0x80 else 0)


def _gif_with_comment(n_bytes: int, block: int = 255) -> bytes:
    """A real GIF with a comment extension of n_bytes inserted after its header."""
    data = _gif()
    at = _gif_header_len(data)
    ext = bytearray(b"\x21\xfe")
    left = n_bytes
    while left > 0:
        k = min(block, left)
        ext += bytes([k]) + b"c" * k
        left -= k
    ext += b"\x00"
    return data[:at] + bytes(ext) + data[at:]


@pytest.fixture
def no_pillow_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow must never see a refused file: the precheck is what stops it."""

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("Image.open reached")

    monkeypatch.setattr(image_stage.Image, "open", boom)


def test_apng_bomb_is_refused_before_pillow_opens_it(no_pillow_open: None) -> None:
    bomb = _apng_bomb()
    assert len(bomb) < 200
    with pytest.raises(ValueError, match="image too large"):
        image_stage.features(bomb)
    res = image_stage.analyze(bomb, [], 8)
    assert res.features is None and res.error and "image too large" in res.error
    with pytest.raises(ValueError, match="image too large"):
        vision.preprocess(bomb)


def test_apng_bomb_is_refused_by_ocr(monkeypatch: pytest.MonkeyPatch, no_pillow_open: None) -> None:
    monkeypatch.setattr(ocr, "_get_engine", lambda: object())
    assert ocr._read(_apng_bomb()) == ([], "image too large")


def test_oversized_gif_screen_and_frame_are_refused(no_pillow_open: None) -> None:
    data = bytearray(_gif())
    data[6:10] = struct.pack("<HH", 60000, 60000)  # logical screen
    with pytest.raises(ValueError, match="image too large"):
        image_stage.precheck(bytes(data))
    data = bytearray(_gif())
    desc = data.index(b"\x2c", _gif_header_len(bytes(data)))  # first image descriptor
    data[desc + 5 : desc + 9] = struct.pack("<HH", 60000, 60000)
    with pytest.raises(ValueError, match="image too large"):
        image_stage.precheck(bytes(data))


def test_gif_comment_bomb_is_refused_fast(no_pillow_open: None) -> None:
    """GHSA-74r2-7ff9-mjj2: a 5 MB GIF of comment sub-blocks cost Pillow 4 s per open."""
    bomb = _gif_with_comment(5_000_000, block=1)
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match="gif comment too large"):
        image_stage.precheck(bomb)
    assert time.perf_counter() - t0 < 0.5


def test_gif_frame_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_stage, "GIF_MAX_FRAMES", 3)
    image_stage.precheck(_gif(frames=3))
    with pytest.raises(ValueError, match="too many frames"):
        image_stage.precheck(_gif(frames=4))


def test_oversized_webp_canvas_is_refused(no_pillow_open: None) -> None:
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
    vp8x += (16383).to_bytes(3, "little") + (16383).to_bytes(3, "little")
    data = b"RIFF" + struct.pack("<I", 4 + len(vp8x)) + b"WEBP" + vp8x
    with pytest.raises(ValueError, match="image too large"):
        image_stage.precheck(data)


def test_real_logos_pass_the_precheck_unchanged() -> None:
    """The precheck changes nothing for ordinary logos: still hashed, animated GIFs counted."""
    image_stage.precheck(PNG)
    f = image_stage.features(PNG)
    assert (f.width, f.height, f.format) == (64, 64, "PNG")
    g = image_stage.features(_gif(frames=3))
    assert g.animated and g.frames == 3
    small_comment = _gif_with_comment(1000)
    assert image_stage.features(small_comment).format == "GIF"
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, format="WEBP")
    image_stage.precheck(buf.getvalue())
    assert image_stage.features(buf.getvalue()).width == 40
    assert vision.preprocess(PNG).shape == (1, 3, vision.SIZE, vision.SIZE)


# ------------------------------------------------- TS-1: fetch_tweet takes answers as they come


def _mirror(status: str | None, delay: float, source: str, calls: list[str]) -> Any:
    async def fn(http: Any, tweet_id: str) -> xs.TweetData | None:
        calls.append(source)
        await asyncio.sleep(delay)
        if status is None:
            return None
        return xs.TweetData(id=tweet_id, status=status, source=source, text=source)

    fn.__name__ = source
    return fn


async def test_slow_fxtwitter_no_longer_blocks_good_mirror_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(xs, "FETCH_TWEET_GRACE_S", 0.2)
    monkeypatch.setattr(xs, "fx_tweet", _mirror("ok", 5.0, "fxtwitter", calls))
    monkeypatch.setattr(xs, "vx_tweet", _mirror("ok", 0.05, "vxtwitter", calls))
    monkeypatch.setattr(xs, "syndication_tweet", _mirror(None, 0.1, "syndication", calls))
    t0 = time.perf_counter()
    t = await xs.fetch_tweet(None, "1")  # type: ignore[arg-type]
    assert t.status == "ok" and t.source == "vxtwitter"
    assert time.perf_counter() - t0 < 1.0


async def test_fast_fxtwitter_is_still_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outputs are as before when FxTwitter answers within the grace."""
    calls: list[str] = []
    monkeypatch.setattr(xs, "FETCH_TWEET_GRACE_S", 1.0)
    monkeypatch.setattr(xs, "fx_tweet", _mirror("ok", 0.2, "fxtwitter", calls))
    monkeypatch.setattr(xs, "vx_tweet", _mirror("ok", 0.0, "vxtwitter", calls))
    monkeypatch.setattr(xs, "syndication_tweet", _mirror("ok", 0.0, "syndication", calls))
    t = await xs.fetch_tweet(None, "1")  # type: ignore[arg-type]
    assert t.source == "fxtwitter"


async def test_deleted_verdict_waits_for_an_ok_and_is_kept_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(xs, "fx_tweet", _mirror("deleted", 0.0, "fxtwitter", calls))
    monkeypatch.setattr(xs, "vx_tweet", _mirror("ok", 0.1, "vxtwitter", calls))
    monkeypatch.setattr(xs, "syndication_tweet", _mirror(None, 0.0, "syndication", calls))
    assert (await xs.fetch_tweet(None, "1")).source == "vxtwitter"  # type: ignore[arg-type]
    monkeypatch.setattr(xs, "vx_tweet", _mirror("deleted", 0.0, "vxtwitter", calls))
    monkeypatch.setattr(xs, "oembed_tweet", _mirror(None, 0.0, "oembed", calls))
    t = await xs.fetch_tweet(None, "1")  # type: ignore[arg-type]
    assert t.status == "deleted" and t.source == "fxtwitter"


@needs_db
async def test_basic_read_timeout_leaves_the_fetch_to_store_the_post(
    migrated_db: str, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A basic read that timed out cancelled the fetch before its insert, so every later
    basic read of the post repeated the same failure."""
    calls = 0

    async def slow_fetch(http: Any, tweet_id: str, **_: Any) -> xs.TweetData:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.4)
        return xs.TweetData(id=tweet_id, status="ok", source="fxtwitter", text="gm")

    monkeypatch.setattr(fulldepth, "fetch_tweet", slow_fetch)
    s = Settings(_env_file=None, basic_x_timeout_s=0.1)  # type: ignore[call-arg]
    post = analyzer._x_info("https://x.com/someone/status/777001", None)
    assert post is not None
    from tokensage.db import create_pool

    pool = await create_pool(migrated_db, min_size=2, max_size=4)
    try:

        async def basic() -> fulldepth.BasicXLoad:
            async with pool.acquire() as c:
                return await fulldepth.basic_x_load(c, None, s, post, pool=pool)  # type: ignore[arg-type]

        first = await asyncio.gather(basic(), basic())
        assert all(b.tweet is None and "timed out" in b.caveats[0] for b in first)
        for _ in range(50):
            if not fulldepth._basic_tweet_fetches:
                break
            await asyncio.sleep(0.05)
        again = await basic()
    finally:
        await pool.close()
    assert calls == 1  # one shared background fetch for both reads
    assert again.tweet is not None and again.tweet.text == "gm" and not again.caveats


# ------------------------------------------------- TS-2: media_hashes cancels its siblings


async def test_media_hashes_cancels_other_downloads_on_a_write_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []

    class FakeConn:
        async def fetchrow(self, *_: Any, **__: Any) -> None:
            return None

        async def execute(self, sql: str, *args: Any, **__: Any) -> str:
            writes.append(args[0])
            if args[0].endswith("a.jpg"):
                raise RuntimeError("db error on a")
            return "INSERT 0 1"

    from tokensage.resolve import metadata as md

    async def fake_fetch_url(http: Any, url: str, settings: Any, **_: Any) -> Any:
        await asyncio.sleep(0.02 if "a.jpg" in url else 0.3)
        raise FetchError("http 404", 404, False)

    monkeypatch.setattr(md, "fetch_url", fake_fetch_url)
    urls = ["https://pbs.twimg.com/a.jpg", "https://pbs.twimg.com/b.jpg"]
    with pytest.raises(RuntimeError, match="db error on a"):
        await fulldepth.media_hashes(FakeConn(), None, Settings(_env_file=None), urls)  # type: ignore[arg-type,call-arg]
    await asyncio.sleep(0.5)
    assert writes == [urls[0]]  # b's insert never runs on the failed job's connection


# ------------------------------------------------- TS-3: waiters share the leader's failure


async def test_story_heat_waiters_reuse_the_leaders_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = 0

    async def failing_search(http: Any, story: str) -> None:
        nonlocal searches
        searches += 1
        await asyncio.sleep(0.1)
        return None

    monkeypatch.setattr(geckoterminal, "search_pools", failing_search)
    good = StoryHeat(story="moo deng", story_kind="referent", status="ok", as_of=None)  # type: ignore[arg-type]
    writes: list[Any] = []

    class FakeConn:
        async def fetchrow(self, *_: Any) -> dict[str, Any]:
            return {
                "value": good.model_dump(mode="json"),
                "fetched_at": datetime.now(UTC) - timedelta(minutes=6),  # past HEAT_TTL
            }

        async def execute(self, *a: Any) -> str:
            writes.append(a)
            return "INSERT 0 1"

    doc = {"referent": {"label": "Moo Deng", "generic": False, "source": "entities:x"}}
    t0 = time.perf_counter()
    heats = await asyncio.gather(*(now.story_heat(FakeConn(), None, doc) for _ in range(6)))  # type: ignore[arg-type]
    assert searches == 1
    assert time.perf_counter() - t0 < 0.5
    assert all(h is not None and h.status == "ok" for h in heats)
    assert not writes


def test_heat_fallback_never_outlives_the_stale_row() -> None:
    heat = StoryHeat(story="x", story_kind="referent", status="ok", as_of=None)  # type: ignore[arg-type]
    now._fallback_keep("k", heat, now.HEAT_STALE_MAX + timedelta(seconds=1))
    assert now._fallback_get("k") is None
    now._fallback_keep("k", heat, now.HEAT_STALE_MAX - timedelta(seconds=30))
    assert now._heat_fallback["k"][0] - time.monotonic() <= 30


# ------------------------------------------------- TS-4: memo before the DexScreener breaker


async def test_open_dexscreener_breaker_still_serves_the_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        SameNameToken(mint="M1", name="Moo", symbol="MOO", created_at=None, source="dexscreener")
    ]
    lookups.search_memo.done["moo"] = (time.monotonic() + 60, hits)
    for _ in range(breaker.threshold):
        breaker.failure("dexscreener_search")
    assert not breaker.allow("dexscreener_search")

    async def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("network while the breaker is open")

    monkeypatch.setattr(lookups, "_dexscreener_fetch", no_network)
    got = await analyzer._external_namesakes(None, "MOO", "moo")  # type: ignore[arg-type]
    assert [g.mint for g in got] == ["M1"]
    assert analyzer._ext_lookups_allowed("MOO")
    assert not analyzer._ext_lookups_allowed("OTHER")  # no memo: still gated
    assert await analyzer._external_namesakes(None, "OTHER", "other") == []  # type: ignore[arg-type]
