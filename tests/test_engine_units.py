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


def test_copycat_only_counts_coins_from_the_copycat_window() -> None:
    from datetime import UTC, datetime, timedelta

    from tokensage.engine.pipeline import SameNameToken

    now = datetime(2026, 10, 1, tzinfo=UTC)

    def run(*ages_days: float):  # type: ignore[no-untyped-def]
        same = [
            SameNameToken(f"m{i}", "Blorbo", "BLORBO", now - timedelta(days=d), "db")
            for i, d in enumerate(ages_days)
        ]
        ctx = DbContext(same_name=same)
        return run_basic(EngineInput("mine", "Blorbo", "BLORBO", None, None, now, ctx=ctx))

    old = run(45, 90)  # only months-old namesakes: not a copy of a live coin
    assert not {"copycat", "earlier_same_name"} & {f.code for f in old.flags}
    assert "derivative/copycat" not in dict(old.agg.categories)
    assert old.copy_of == []

    live = run(90, 6)  # a namesake launched 6 days earlier: the copied coin
    copy = next(f for f in live.flags if f.code == "copycat")
    assert "within 30 d" in copy.detail
    assert live.copy_of[0]["mint"] == "m1" and live.copy_of[0]["recent"] is True

    just_now = run(0.001)  # a few minutes apart: launched together, not a copy
    assert "copycat" not in {f.code for f in just_now.flags}


def test_logo_copy_needs_an_earlier_recent_token() -> None:
    from datetime import UTC, datetime, timedelta

    from tests.fixtures.chain import PNG
    from tokensage.engine import image as image_stage

    now = datetime(2026, 10, 1, tzinfo=UTC)
    f = image_stage.features(PNG)
    assert f is not None

    def run(created: datetime):  # type: ignore[no-untyped-def]
        cand = image_stage.Candidate("ipfs:x", f.phash, mint="OtherMint111", created_at=created)
        ctx = DbContext(image_candidates=[cand])
        return run_basic(EngineInput("mine", "Zorp", "ZORP", None, PNG, now, ctx=ctx))

    earlier = run(now - timedelta(days=3))
    assert "copycat" in {fl.code for fl in earlier.flags}
    later = run(now + timedelta(hours=2))  # that token copied this one, not the reverse
    assert "copycat" not in {fl.code for fl in later.flags}
    assert not any(e.kind == "image_hash" for e in later.evidence)


def test_cached_logo_hashes_are_still_compared() -> None:
    from datetime import UTC, datetime, timedelta

    from tests.fixtures.chain import PNG
    from tokensage.engine import image as image_stage

    now = datetime(2026, 10, 1, tzinfo=UTC)
    f = image_stage.features(PNG)
    assert f is not None
    cand = image_stage.Candidate(
        "ipfs:x", f.phash, mint="OtherMint111", created_at=now - timedelta(days=1)
    )
    inp = EngineInput(
        "mine", "Zorp", "ZORP", None, None, now, ctx=DbContext(image_candidates=[cand])
    )
    inp.logo_features = f  # no image bytes this run: hashes from cache
    out = run_basic(inp)
    assert out.image.near and any(e.kind == "image_hash" for e in out.evidence)


# ----------------------------------------------------------------- dictionary senses vs names


def _basic(name: str, symbol: str, description: str | None = None):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    return run_basic(
        EngineInput(
            mint="So11111111111111111111111111111111111111112",
            name=name,
            symbol=symbol,
            description=description,
            image_bytes=None,
            created_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    )


def test_dictionary_only_label_is_capped_and_name_word_halved() -> None:
    k = load_knowledge()
    out = _basic("Grok Companion Ani", "ANI")
    cats = dict(out.agg.categories)
    assert cats.get("animal/bird", 0) < k.scoring["wordnet_only_cap"]
    assert cats["ai_agent"] > cats.get("animal/bird", 0)
    bird = [e for e in out.evidence if e.label == "animal/bird" and e.kind == "wordnet"]
    assert bird and bird[0].weight <= 0.55 * k.scoring["name_word_factor"] + 1e-9
    assert "given name" in bird[0].detail
    # the framing follows the scores: not "a bird coin tied to Grok"
    assert "bird coin" not in out.summary
    assert "refers to Grok" in out.summary


def test_dictionary_only_cap_keeps_plain_animal_coins() -> None:
    out = _basic("Zeus the Dog", "ZEUS")
    cats = dict(out.agg.categories)
    assert 0.5 <= cats["animal/dog"] <= load_knowledge().scoring["wordnet_only_cap"]
    assert out.agg.categories[0][0] == "animal"


def test_baby_marker_needs_something_to_derive_from() -> None:
    shark = _basic("Baby Shark", "SHARK")
    assert not any(lbl.startswith("derivative") for lbl, _ in shark.agg.categories)
    assert not any(e.label.startswith("derivative/") for e in shark.evidence)
    trump = _basic("Baby Trump", "BTRUMP")
    assert "derivative/template_family" in dict(trump.agg.categories)
    pnut = _basic("Baby PNUT", "BPNUT")
    assert "derivative/template_family" in dict(pnut.agg.categories)


def test_ticker_only_coin_match_does_not_inherit_its_subject() -> None:
    out = _basic("Department of Government Efficiency", "DOGE")
    cats = dict(out.agg.categories)
    assert "animal/dog" not in cats
    assert out.agg.referent is not None and "Efficiency" in out.agg.referent.label
    # the pun on Dogecoin is still reported as a reference
    assert any(e.kind == "known_coin" and e.source == "known_coins:DOGE" for e in out.evidence)
    assert {c["ticker"] for c in out.copy_of} >= {"DOGE"}
    # a name that *contains* the coin keeps inheriting ("Mini Doge" is a dog coin)
    mini = _basic("Mini Doge", "MDOGE")
    assert "animal/dog" in dict(mini.agg.categories)


def test_description_only_referent_is_a_weak_guess() -> None:
    out = _basic("Gork", "GORK", "elons dumb ai")
    assert out.agg.referent is not None and out.agg.referent.score < 0.45
    assert any("appears only in the description" in c for c in out.caveats)
    assert "weak guess: Elon Musk" in out.summary
    # the name itself naming the entity is unaffected
    named = _basic("Elon's Cat", "ECAT")
    assert named.agg.referent is not None and named.agg.referent.score >= 0.6
