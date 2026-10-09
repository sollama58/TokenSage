"""Audit 0.28, media/X group: degenerate logos in OCR and the 16-bit image path, camelCase and
subtitled trend labels, the one-word referent fallback, boundary-aware cashtags, accounts
made after the token, the bounded capitalised-name scan, and the contract address in a post."""

from __future__ import annotations

import io
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from tokensage.engine import image as image_stage
from tokensage.engine import ocr, trends, vision, wikilookup, xmatch, xsignals
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.pipeline import EngineInput, run_full
from tokensage.sources import x as xs

TOKEN_T = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MINT = "So11111111111111111111111111111111111111112"


def _png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (30, 60, 120) if mode == "RGB" else 0).save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------- XM-1: thin strips in OCR


def test_ocr_skips_degenerate_shapes_without_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, ...]] = []

    def engine(arr: np.ndarray) -> tuple[list[object], float]:
        calls.append(arr.shape)
        return [], 0.0

    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    for size in ((10000, 10), (10, 10000), (640, 4), (1, 640)):
        lines, err = ocr.read(_png(size))
        assert lines == [] and err is not None and "too thin" in err, (size, err)
    assert calls == []  # never reached the recogniser
    assert ocr.read(_png((640, 40))) == ([], None)  # 16:1 is still read
    assert ocr.read(_png((1024, 32))) == ([], None)  # a wide wordmark (32:1) too
    assert ocr.read(_png((300, 300))) == ([], None)
    assert calls == [(40, 640, 3), (20, 640, 3), (300, 300, 3)]


@pytest.mark.skipif(not ocr.available(), reason="rapidocr not installed")
def test_ocr_thin_strip_returns_in_under_a_second() -> None:
    data = _png((10000, 10))
    assert len(data) < 1000  # a sub-KB logo used to hold the OCR slot for minutes
    ocr._get_engine()  # the model load is not what is measured
    t0 = time.perf_counter()
    lines, err = ocr.read(data)
    assert time.perf_counter() - t0 < 1.0
    assert lines == [] and err is not None and "too thin" in err


def test_ocr_time_budget_gives_up_on_a_slow_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(arr: np.ndarray) -> tuple[list[object], float]:
        time.sleep(0.5)
        return [], 0.0

    monkeypatch.setattr(ocr, "_get_engine", lambda: slow)
    monkeypatch.setattr(ocr, "TIME_BUDGET_S", 0.05)
    t0 = time.perf_counter()
    lines, err = ocr.read(_png((300, 300)))
    assert time.perf_counter() - t0 < 0.4
    assert lines == [] and err is not None and "gave up" in err
    monkeypatch.setattr(ocr, "TIME_BUDGET_S", 5.0)
    assert ocr.read(_png((300, 300))) == ([], None)


# ----------------------------------------------------------------- SEC-4: 16-bit PNG memory


def _png16(side: int = 6300) -> bytes:
    y = np.linspace(0, 65535, side, dtype=np.float64)
    arr = np.broadcast_to(y[:, None], (side, side)).astype(np.uint16)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_16bit_png_is_scaled_before_the_float_conversion() -> None:
    data = _png16()
    assert len(data) < image_stage.MAX_PIXELS and 6300 * 6300 < image_stage.MAX_PIXELS
    with Image.open(io.BytesIO(data)) as img:
        assert img.mode == "I;16"
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        res = image_stage.analyze(data, [], 14)
        elapsed = time.perf_counter() - t0
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert res.error is None and res.features is not None
    assert res.features.width == 6300 and res.features.height == 6300
    assert peak < 120 * 1024 * 1024, peak  # the float64 full frame alone was 317 MB
    assert elapsed < 5.0
    # the gradient still hashes as a gradient, not a flat near-white frame
    flat = image_stage.features(_png((300, 300), "L"))
    assert res.features.phash != flat.phash


def test_vision_preprocess_accepts_a_large_16bit_png() -> None:
    pixels = vision.preprocess(_png16())  # thumbnail() on I;16 used to raise
    assert pixels.shape == (1, 3, vision.SIZE, vision.SIZE)
    assert float(pixels.min()) < -0.5 < 0.5 < float(pixels.max())  # dark top, bright bottom


# ----------------------------------------------------------------- XM-2: camelCase labels


def _wiki(term: str, spike: float = 25.0) -> trends.TrendTerm:
    return trends.TrendTerm(term, spike, 500_000)


def _trend_run(
    name: str, symbol: str, terms: list[trends.TrendTerm], desc: str | None = None
) -> list[trends.TrendHit]:
    idx = trends.TrendIndex(terms, load_knowledge())
    return run_full(EngineInput("m", name, symbol, desc, None, None, trend_index=idx)).trend_hits


def test_camelcase_one_word_label_matches_the_split_name() -> None:
    idx = trends.TrendIndex([_wiki("DeepSeek"), _wiki("iPhone", 3.0)], load_knowledge())
    assert normalize("DeepSeek Cat", "CAT", None).name_tokens == ["deep", "seek", "cat"]
    assert [h.term.term for h in idx.match("deep seek cat", "name")] == ["DeepSeek"]
    assert [h.term.term for h in idx.match("deepseek", "name")] == ["DeepSeek"]
    for name, symbol, term in (
        ("DeepSeek Cat", "CAT", "DeepSeek"),
        ("YouTube Cat", "CAT", "YouTube"),
        ("iPhone", "IPHONE", "iPhone"),
    ):
        hits = _trend_run(name, symbol, [_wiki(term)])
        assert [(h.term.term, h.where, h.via) for h in hits] == [(term, "name", None)], name


# ----------------------------------------------------------------- XM-3: one-word referent


def test_one_word_name_falls_back_to_the_referent_hit_when_its_partial_hit_is_gated() -> None:
    trump = [_wiki("Donald Trump", 10.0)]
    for name, symbol in (("Trump", "TRUMP"), ("Trump Dog", "TDOG"), ("Zorb", "TRUMP")):
        hits = _trend_run(name, symbol, trump)
        assert [(h.term.term, h.via, h.partial) for h in hits] == [
            ("Donald Trump", "referent", False)
        ], name
    hits = _trend_run("Musk", "MUSK", [_wiki("Elon Musk", 10.0)])
    assert [(h.term.term, h.via) for h in hits] == [("Elon Musk", "referent")]
    # a partial hit that survives the gate stays partial (the golden WEST and Elon cases)
    x = trends.TrendTerm("Kanye West", 0.0, 8, source="x_trends", rank=2)
    hits = _trend_run("WEST", "WEST", [x], desc="kanye said it first")
    assert [(h.term.term, h.partial, h.via) for h in hits] == [("Kanye West", True, None)]
    hits = _trend_run("Elon", "ELON", [_wiki("Elon Musk", 10.0)])
    assert [(h.term.term, h.partial, h.via) for h in hits] == [("Elon Musk", True, None)]
    assert _trend_run("WEST", "WEST", [x]) == []


# ----------------------------------------------------------------- XM-5: subtitled titles


def test_colon_titles_keep_their_surfaces_but_namespaces_do_not() -> None:
    assert trends.surfaces_for("Dune: Part Two") == ["dune part two"]
    assert trends.surfaces_for("Spider-Man: No Way Home") == ["spider man no way home"]
    for ns in ("Special:Search", "Wikipedia:Main Page", "Category talk:Films", "File:X.png"):
        assert trends.surfaces_for(ns) == [], ns
    hits = _trend_run("Dune Part Two", "DUNE", [_wiki("Dune: Part Two", 12.0)])
    assert [(h.term.term, h.surface) for h in hits] == [("Dune: Part Two", "dune part two")]


# ----------------------------------------------------------------- XM-4 / XM-6: xsignals


def _tweet(text: str, created: datetime, joined: datetime | None = None) -> xs.TweetData:
    return xs.TweetData(
        id="1",
        status="ok",
        source="syndication",
        text=text,
        created_at=created,
        author_handle="dev",
        author_id="1",
        author_name="dev",
        followers=50,
        author_joined=joined or TOKEN_T - timedelta(days=400),
    )


def test_cashtag_mention_is_the_whole_cashtag() -> None:
    before = TOKEN_T - timedelta(days=2)

    def relation(text: str, ticker: str) -> tuple[str | None, list[str]]:
        a = xsignals.assess("tweet", "dev", _tweet(text, before), None, TOKEN_T, ticker, MINT, [])
        return a.relation, [e.kind for e in a.evidence]

    assert relation("$solana to the moon", "SOL") == ("narrative_reference", ["x_timing"])
    assert relation("$PEPEX is the one", "PEPE") == ("narrative_reference", ["x_timing"])
    assert relation("$a", "A") == ("official_account", ["x_mentions"])
    assert relation("$SOL to the moon", "SOL") == ("official_account", ["x_mentions"])
    assert relation("$sol, finally", "SOL") == ("official_account", ["x_mentions"])


def test_account_made_after_the_token_is_fresh() -> None:
    p = xs.ProfileData(
        handle="randomguy",
        status="ok",
        followers=3,
        joined=TOKEN_T + timedelta(hours=2),
        name="Random",
    )
    a = xsignals.assess("profile", "randomguy", None, p, TOKEN_T, "ZORB", MINT, ["zorb"])
    assert [(c, d) for c, _s, d in a.flags] == [
        ("fresh_x_account", "@randomguy was created 2.0 h after the token")
    ]
    t = _tweet("launching now", TOKEN_T + timedelta(minutes=5), TOKEN_T + timedelta(minutes=2))
    a = xsignals.assess("tweet", "dev", t, None, TOKEN_T, "ZORB", MINT, ["zorb"])
    assert ("fresh_x_account", "info", "@dev was created 2 min after the token") in a.flags
    p.joined = TOKEN_T - timedelta(days=13)
    a = xsignals.assess("profile", "randomguy", None, p, TOKEN_T, "ZORB", MINT, ["zorb"])
    assert a.flags[0][2] == "@randomguy was created 13.0 d before the token"
    p.joined = TOKEN_T - timedelta(days=400)
    assert xsignals.assess("profile", "randomguy", None, p, TOKEN_T, "Z", MINT, []).flags == []


# ----------------------------------------------------------------- SEC-1: the name scan


def test_capitalised_name_scan_is_linear_and_capped() -> None:
    hostile = "Ab" * 12500
    t0 = time.perf_counter()
    assert list(wikilookup._CAP_SPAN.finditer(hostile)) == []
    assert time.perf_counter() - t0 < 0.3  # was 4-9 s (quadratic)
    k = load_knowledge()
    n = normalize("Test Coin", "TST", None)
    t0 = time.perf_counter()
    wikilookup.spans(n, [hostile, hostile + " Zorbulon Vexley"], k, None)
    assert time.perf_counter() - t0 < 0.5
    # the cap: a name past 4,000 characters is not scanned, one before it is
    far = "x " * 2100 + "Zorbulon Vexley"
    assert [s.text for s in wikilookup.spans(n, [far], k, None)] == []
    near = "Zorbulon Vexley " + "x " * 2100
    assert [s.text for s in wikilookup.spans(n, [near], k, None)] == ["zorbulon vexley"]
    # ordinary names still read as before
    found = [
        m.group(0)
        for m in wikilookup._CAP_SPAN.finditer(
            "met JPMorgan Chase and O'Brien McDonald in Rio de Janeiro today"
        )
    ]
    assert found == ["JPMorgan Chase", "O'Brien McDonald", "Rio de Janeiro"]


# ----------------------------------------------------------------- EC-2: the contract address


def _x_run(text: str, name: str = "Zorblax", symbol: str = "ZORB") -> object:
    tweet = xs.TweetData(
        id="1",
        status="ok",
        source="fxtwitter",
        text=text,
        created_at=TOKEN_T - timedelta(hours=1),
        author_handle="zorbdev",
        followers=300,
        author_joined=TOKEN_T - timedelta(days=900),
    )
    return run_full(
        EngineInput(
            mint=MINT,
            name=name,
            symbol=symbol,
            description=None,
            image_bytes=None,
            created_at=TOKEN_T,
            x_kind="tweet",
            x_url_handle="zorbdev",
            tweet=tweet,
            ocr_lines=[],
            run_ocr=False,
            x_media=[],
        )
    )


def test_post_with_the_contract_address_matches_the_token() -> None:
    for text in (f"launching {MINT} now", f"CA: {MINT}", f"https://pump.fun/coin/{MINT}"):
        out = _x_run(text)
        m = out.x_match
        assert m is not None and out.x is not None and out.x.relation == "official_account", text
        assert m.verdict == "about_this_coin" and m.fit >= 0.95, (text, m.fit)
        assert m.ticker.how == "contract" and m.basis == ["post_text"], (text, m.ticker, m.basis)
        assert "x_content_mismatch" not in {f.code for f in out.flags}, text
    out = _x_run(f"$ZORB is live {MINT}")
    assert out.x_match is not None and out.x_match.basis == ["post_text", "cashtag"]
    assert out.x_match.ticker.detail == "the post carries the contract address and names $ZORB"
    # another coin's address, or a prefix of this one, is not this coin
    other = "So11111111111111111111111111111111111111113"
    out = _x_run(f"CA: {other}")
    assert out.x_match is not None and out.x_match.verdict == "unrelated"
    assert "x_content_mismatch" in {f.code for f in out.flags}
    assert not xmatch.contract_in_post(MINT, f"x{MINT}")
    assert not xmatch.contract_in_post(MINT[:20], MINT)
    assert not xmatch.contract_in_post(MINT, f"ca: {MINT.lower()}!")  # mints are case-sensitive
