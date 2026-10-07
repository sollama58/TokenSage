"""Golden cases for x.match: the linked X post/profile vs the token (no network, no DB)."""

from __future__ import annotations

import io
import random
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image, ImageDraw

from tokensage.engine import image as image_stage
from tokensage.engine.pipeline import EngineInput, run_full
from tokensage.engine.xmatch import MediaHash
from tokensage.sources.x import ProfileData, TweetData

CASES: list[dict[str, Any]] = yaml.safe_load(
    (Path(__file__).parent / "golden" / "x_cases.yaml").read_text()
)["cases"]
TOKEN_T = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@lru_cache
def img(spec: str) -> bytes:
    """'img:<seed>' -> a deterministic blocky pattern; 'img:<seed>:edit' -> the same with a
    small overlay, like a logo cut from a post screenshot with a caption added."""
    parts = spec.split(":")
    seed = int(parts[1])
    rnd = random.Random(seed)
    im = Image.new("RGB", (256, 256), (rnd.randrange(256), rnd.randrange(256), 200))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x0, y0 = rnd.randrange(0, 200), rnd.randrange(0, 200)
        d.rectangle(
            [x0, y0, x0 + rnd.randrange(20, 90), y0 + rnd.randrange(20, 90)],
            fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)),
        )
    if len(parts) > 2 and parts[2] == "edit":
        d.rectangle([4, 228, 120, 252], fill=(255, 255, 255))  # a caption bar
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def media_hash(spec: str) -> MediaHash:
    f = image_stage.features(img(spec))
    return MediaHash(
        url=f"https://pbs.twimg.com/media/{spec}.jpg",
        status="ok",
        phash=f.phash,
        phash_mirror=f.phash_mirror,
    )


def _tweet(t: dict[str, Any] | None, tid: str = "1") -> TweetData | None:
    if t is None:
        return None
    if t.get("deleted"):
        return TweetData(id=tid, status="deleted", source="fxtwitter")
    quoted = _tweet(t["quoted"], "2") if t.get("quoted") else None
    return TweetData(
        id=tid,
        status="ok",
        source="fxtwitter",
        text=t.get("text"),
        created_at=TOKEN_T - timedelta(hours=float(t.get("hours_before_token", 1))),
        author_handle=t.get("author"),
        followers=t.get("followers"),
        verified_type=t.get("verified"),
        author_joined=TOKEN_T - timedelta(days=float(t.get("author_joined_days_before", 900))),
        media_urls=[f"https://pbs.twimg.com/media/{m}.jpg" for m in t.get("media", [])],
        quoted=quoted,
        quoted_tweet_id="2" if quoted else None,
    )


def _run(case: dict[str, Any]):  # type: ignore[no-untyped-def]
    tok = case["token"]
    tweet = _tweet(case.get("tweet")) if case["link"] == "tweet" else None
    profile = None
    media: list[MediaHash] = []
    if case["link"] == "profile":
        p = case["profile"]
        profile = ProfileData(
            handle=p["handle"],
            status="ok",
            source="fxtwitter",
            name=p.get("name"),
            followers=p.get("followers"),
            statuses=p.get("statuses"),
            joined=TOKEN_T - timedelta(hours=float(p.get("joined_hours_before", 30 * 24))),
            description=p.get("bio"),
            avatar_url=f"https://pbs.twimg.com/profile_images/{p['avatar']}.jpg"
            if p.get("avatar")
            else None,
        )
        media = [media_hash(p["avatar"])] if p.get("avatar") else []
    elif tweet is not None and tweet.status == "ok":
        specs = list(case["tweet"].get("media", []))
        if case["tweet"].get("quoted"):
            specs += case["tweet"]["quoted"].get("media", [])
        media = [media_hash(s) for s in specs]
    inp = EngineInput(
        mint="So11111111111111111111111111111111111111112",
        name=tok["name"],
        symbol=tok["symbol"],
        description=tok.get("description"),
        image_bytes=img(tok["logo"]) if tok.get("logo") else None,
        created_at=TOKEN_T,
        x_kind=case["link"],
        x_url_handle=(tweet.author_handle if tweet else profile.handle if profile else None),
        tweet=tweet,
        profile=profile,
        ocr_lines=[],  # no OCR in golden runs
        run_ocr=False,
        x_media=media,
    )
    return run_full(inp)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_x_match(case: dict[str, Any]) -> None:
    out = _run(case)
    m = out.x_match
    assert m is not None, "x_match missing"
    flags = {f.code for f in out.flags}
    e = case["expect"]
    ctx = (
        f"\n  fit={m.fit} verdict={m.verdict}\n  name={m.name}\n  ticker={m.ticker}"
        f"\n  image={m.image}\n  referent={m.referent}\n  x_cats={m.x_categories}\n  flags={flags}"
    )
    if "verdict" in e:
        assert m.verdict == e["verdict"], ctx
    if "fit_min" in e:
        assert m.fit >= e["fit_min"], ctx
    if "fit_max" in e:
        assert m.fit <= e["fit_max"], ctx
    if "name_how" in e:
        assert m.name.how == e["name_how"], ctx
    if "ticker_how" in e:
        assert m.ticker.how == e["ticker_how"], ctx
    if "image_min_score" in e:
        assert m.image.score >= e["image_min_score"], ctx
    if "agrees" in e:
        assert m.referent.agrees is e["agrees"], ctx
    for b in e.get("basis_include", []):
        assert b in m.basis, f"missing basis {b} in {m.basis}{ctx}"
    for b in e.get("basis_exclude", []):
        assert b not in m.basis, f"unexpected basis {b} in {m.basis}{ctx}"
    if "made_for_coin" in e:
        assert out.x_account is not None, ctx
        assert out.x_account.made_for_coin is e["made_for_coin"], f"{out.x_account}{ctx}"
    if "credibility_min" in e:
        assert (out.x_credibility or 0) >= e["credibility_min"], f"{out.x_credibility}{ctx}"
    if "credibility_max" in e:
        assert out.x_credibility is not None, ctx
        assert out.x_credibility <= e["credibility_max"], f"{out.x_credibility}{ctx}"
    for f in e.get("flags_include", []):
        assert f in flags, f"missing flag {f}{ctx}"
    for f in e.get("flags_exclude", []):
        assert f not in flags, f"unexpected flag {f}{ctx}"


def test_synthetic_images_behave_like_logos() -> None:
    a = image_stage.features(img("img:11"))
    assert image_stage.hamming(a.phash, image_stage.features(img("img:11")).phash) == 0
    edited = image_stage.hamming(a.phash, image_stage.features(img("img:11:edit")).phash)
    other = image_stage.hamming(a.phash, image_stage.features(img("img:22")).phash)
    assert edited <= 14 < other, (edited, other)
