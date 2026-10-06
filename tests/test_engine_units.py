"""Unit tests for engine pieces that golden cases don't isolate: image hashing and near-dups,
ticker base stripping, segmentation choices, summary rendering."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageOps

from tokensage.engine import image as im
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize, ticker_base
from tokensage.engine.pipeline import DbContext, EngineInput, run_basic


def _logo(text_color=(250, 220, 40), size=256, rotate=0) -> bytes:  # type: ignore[no-untyped-def]
    img = Image.new("RGB", (size, size), (30, 60, 120))
    d = ImageDraw.Draw(img)
    d.ellipse((size * 0.08, size * 0.2, size * 0.55, size * 0.8), fill=text_color)
    d.rectangle((size * 0.6, size * 0.1, size * 0.9, size * 0.35), fill=(200, 40, 40))
    if rotate:
        img = img.rotate(rotate, fillcolor=(30, 60, 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _as_jpeg(png: bytes, quality: int = 30) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _mirror(png: bytes) -> bytes:
    img = ImageOps.mirror(Image.open(io.BytesIO(png)).convert("RGB"))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_phash_stable_under_resize_and_jpeg_and_mirror() -> None:
    a = im.features(_logo())
    b = im.features(_logo(size=128))
    c = im.features(_as_jpeg(_logo()))
    assert im.hamming(a.phash, b.phash) <= 4
    assert im.hamming(a.phash, c.phash) <= 4
    m = im.features(_mirror(_logo()))
    # plain pHash drifts on mirroring; the mirror hash stays close
    assert im.hamming(a.phash_mirror, m.phash) <= 6
    assert im.hamming(a.phash_mirror, m.phash) < im.hamming(a.phash, m.phash)
    assert a.width == 256 and not a.animated and a.palette_hex and a.palette_names


def test_near_duplicates_and_distance_ranking() -> None:
    a = im.features(_logo())
    other = im.features(_logo(text_color=(20, 200, 60)))
    far = im.features(_logo(rotate=45))
    cands = [
        im.Candidate("k1", other.phash, known_coin="OTHER"),
        im.Candidate("k2", a.phash, mint="SAME"),
        im.Candidate("k3", far.phash, mint="FAR"),
    ]
    res = im.analyze(_logo(), cands, max_distance=14)
    assert res.features is not None and res.near
    assert res.near[0].candidate.mint == "SAME" and res.near[0].distance == 0
    assert all(n.candidate.mint != "FAR" for n in res.near) or res.near[-1].distance > 8


def test_hostile_image_does_not_raise() -> None:
    assert im.analyze(b"not an image at all", [], 14).error
    assert im.analyze(None, [], 14).error
    huge = Image.new("RGB", (10, 10))
    buf = io.BytesIO()
    huge.save(buf, format="PNG")
    assert im.analyze(buf.getvalue(), [], 14).features is not None


def test_ticker_base_strips_affixes() -> None:
    k = load_knowledge()
    assert ticker_base("BPNUT", k)[0] == "PNUT"
    assert ticker_base("PNUT2", k)[0] == "PNUT"
    assert ticker_base("BABYDOGEINU", k)[0] == "DOGE"
    assert ticker_base("WIF", k)[0] == "WIF"  # never below min length
    assert ticker_base("AI", k)[0] == "AI"


def test_segmentation_prefers_meme_vocabulary() -> None:
    assert normalize("dogwifhat", "WIF", None).name_tokens == ["dogwifhat"]
    assert normalize("peanutthesquirrel", "PNUT", None).name_tokens == ["peanut", "the", "squirrel"]
    assert normalize("Elon Musk Tweeted", "EMT", None).name_tokens == ["elon", "musk", "tweeted"]


def test_description_only_evidence_is_discounted() -> None:
    a = run_basic(EngineInput("m", "Zorp", "ZORP", "a coin about a squirrel", None, None))
    b = run_basic(EngineInput("m", "Squirrel", "SQRL", None, None, None))
    ca, cb = dict(a.agg.categories), dict(b.agg.categories)
    assert cb.get("animal/squirrel", 0) > ca.get("animal/squirrel", 0)


def test_image_logo_reuse_adds_derivative_and_referent() -> None:
    logo = _logo()
    f = im.features(logo)
    ctx = DbContext(image_candidates=[im.Candidate("known:WIF", f.phash, known_coin="WIF")])
    out = run_basic(EngineInput("m", "Random Dog", "RDOG", None, logo, None, ctx=ctx))
    cats = dict(out.agg.categories)
    assert cats.get("derivative/logo_reuse", 0) >= 0.8
    assert out.agg.referent and "dogwifhat" in out.agg.referent.label
    assert any("near-duplicate" in e.detail for e in out.evidence)


def test_same_name_earlier_token_flags_copycat() -> None:
    from datetime import UTC, datetime, timedelta

    from tokensage.engine.pipeline import SameNameToken

    now = datetime(2026, 10, 1, tzinfo=UTC)
    ctx = DbContext(
        same_name=[
            SameNameToken("earlier1", "Blorbo", "BLORBO", now - timedelta(hours=3), "db"),
            SameNameToken(
                "earlier2", "Blorbo", "BLORBO", now - timedelta(hours=1), "pumpfun_search"
            ),
        ],
        x_reuse_count=7,
        creator_token_count=12,
    )
    out = run_basic(
        EngineInput("mine", "Blorbo", "BLORBO", None, None, now, x_kind="tweet", ctx=ctx)
    )
    flags = {f.code for f in out.flags}
    assert {"earlier_same_name", "copycat", "x_link_reused", "serial_creator"} <= flags
    assert out.copy_of and out.copy_of[0]["mint"] == "earlier1"
    assert dict(out.agg.categories).get("derivative/copycat", 0) >= 0.5
