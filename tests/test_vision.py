"""Logo labels from the local vision model (ENABLE_CLIP, engine/vision.py). The model itself is
never loaded here: a stub encoder hands back a class's prompt centroid as the image vector."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator

import asyncpg
import httpx
import numpy as np
import pytest
import respx
import yaml
from PIL import Image

from tests.conftest import needs_db
from tests.fixtures.chain import (
    CID_IMG,
    CID_META,
    T22_MINT,
    FakeChain,
    install_web,
    public_resolver,
)
from tokensage.api.schemas import TokenResponse
from tokensage.config import Settings
from tokensage.engine import vision
from tokensage.engine.pipeline import EngineInput, run_basic, run_full
from tokensage.net import safe_fetch
from tokensage.taxonomy import DATA_DIR, category_labels


class StubEncoder:
    """Returns the prompt centroid of one class as the image vector."""

    def __init__(self, cls: str) -> None:
        head = vision.load_head()
        self.vec = head.centroids[head.classes.index(cls)].copy()
        self.calls = 0

    def encode(self, pixels: np.ndarray) -> np.ndarray:
        assert pixels.shape == (1, 3, vision.SIZE, vision.SIZE)
        self.calls += 1
        return self.vec


def _png(mode: str = "RGBA", size: tuple[int, int] = (40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)).save(
        buf, format="PNG"
    )
    return buf.getvalue()


def _result(cls: str, score: float, runner: float = 0.05) -> vision.VisionResult:
    return vision.VisionResult(vision.load_config().model, [(cls, score), ("none", runner)])


# ----------------------------------------------------------------- data files


def test_head_matches_the_yaml() -> None:
    cfg = vision.load_config()
    head = vision.load_head()
    raw = yaml.safe_load((DATA_DIR / "vision_labels.yaml").read_text("utf-8"))
    assert head.classes == list(raw["classes"])  # same classes, same order
    k = len(head.classes)
    assert head.centroids.shape[0] == k and head.w.shape == (head.centroids.shape[1], k)
    assert head.b.shape == (k,)
    assert np.allclose(np.linalg.norm(head.centroids, axis=1), 1.0, atol=1e-3)
    # every cutoff belongs to a class that emits a taxonomy label
    assert set(cfg.thresholds) <= {c for c, lbl in cfg.classes.items() if lbl}
    assert {lbl for lbl in cfg.classes.values() if lbl} <= category_labels()
    assert cfg.max_weight <= vision.MAX_WEIGHT


def test_labelled_logos_use_known_classes() -> None:
    rows = yaml.safe_load((DATA_DIR.parent / "tests/golden/vision_logos.yaml").read_text("utf-8"))
    classes = set(vision.load_config().classes)
    seen = {c for r in rows["logos"] for c in r["labels"]}
    assert seen <= classes
    # a duplicate group never straddles the split
    split: dict[int, set[str]] = {}
    for r in rows["logos"]:
        split.setdefault(r["group"], set()).add(r["split"])
    assert all(len(s) == 1 for s in split.values())


# ----------------------------------------------------------------- scoring and evidence


@pytest.mark.parametrize("cls", ["dog", "cat", "pepe_wojak", "text_logo"])
def test_a_class_centroid_is_scored_as_that_class(cls: str) -> None:
    res = vision.classify(StubEncoder(cls).vec)
    assert res.top[0][0] == cls
    assert len(res.top) == vision.TOP_K
    assert all(0.0 <= s <= 1.0 for _, s in res.top)


def test_evidence_row_for_an_emitting_class() -> None:
    (ev,) = vision.evidence(_result("dog", 0.9))
    assert ev.kind == "vision" and ev.where == "image" and ev.label == "animal/dog"
    assert ev.referent is None  # a guess at the subject, never who it is
    assert 0.25 <= ev.weight <= vision.MAX_WEIGHT
    assert "looks like a dog" in ev.detail
    # weight grows with the score, from the floor at the cutoff to the cap at 1.0
    thr = vision.load_config().thresholds["dog"]
    (at_cut,) = vision.evidence(_result("dog", thr))
    (full,) = vision.evidence(_result("dog", 1.0))
    assert at_cut.weight == 0.25 and full.weight == vision.MAX_WEIGHT


def test_no_evidence_for_sinks_low_scores_or_another_model() -> None:
    assert vision.evidence(_result("text_logo", 0.99)) == []  # a sink: no taxonomy label
    assert vision.evidence(_result("elon", 0.99)) == []  # no cutoff reached 90% precision
    thr = vision.load_config().thresholds["cat"]
    assert vision.evidence(_result("cat", thr - 0.01)) == []
    old = vision.VisionResult("some-older-head/0", [("dog", 0.99)])
    assert vision.evidence(old) == []  # cached by a different head: ignored
    assert vision.evidence(None) == []
    assert vision.evidence(vision.VisionResult("x", error="decode failed")) == []


# ----------------------------------------------------------------- images


@pytest.mark.parametrize(
    "data",
    [
        _png("RGBA"),
        _png("RGB", (1, 1)),
        _png("P", (300, 20)),
    ],
    ids=["transparent", "one_pixel", "palette_wide"],
)
def test_preprocess_shapes_any_image(data: bytes) -> None:
    px = vision.preprocess(data)
    assert px.shape == (1, 3, vision.SIZE, vision.SIZE) and px.dtype == np.float32
    assert -1.0 <= float(px.min()) and float(px.max()) <= 1.0


def test_preprocess_uses_the_first_frame_of_a_gif() -> None:
    buf = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    px = vision.preprocess(buf.getvalue())
    assert px[0, 0].mean() > px[0, 2].mean()  # red channel dominates: frame one


def test_label_image_never_raises() -> None:
    assert vision.label_image(StubEncoder("cat"), _png()).top[0][0] == "cat"
    res = vision.label_image(StubEncoder("cat"), b"not an image")
    assert res.top == [] and res.error


def test_json_round_trip() -> None:
    res = _result("dog", 0.8)
    assert vision.from_json(vision.to_json(res)) == res
    assert vision.from_json(None) is None and vision.from_json({"model": "x"}) is None


def test_default_encoder_off_or_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    off = Settings(enable_clip=False, vision_model_path=str(tmp_path), _env_file=None)  # type: ignore[call-arg]
    assert vision.default_encoder(off) is None
    missing = Settings(enable_clip=True, vision_model_path=str(tmp_path), _env_file=None)  # type: ignore[call-arg]
    assert vision.default_encoder(missing) is None
    assert "no ONNX model" in (vision.load_error() or "")


# ----------------------------------------------------------------- engine


def _inp(
    res: vision.VisionResult | None, name: str = "Barkley", symbol: str = "BARK"
) -> EngineInput:
    return EngineInput(
        mint="So11111111111111111111111111111111111111112",
        name=name,
        symbol=symbol,
        description=None,
        image_bytes=None,
        created_at=None,
        vision=res,
    )


def test_full_depth_adds_the_logo_guess_to_the_categories() -> None:
    out = run_full(_inp(_result("dog", 0.95)))
    rows = [e for e in out.evidence if e.kind == "vision"]
    assert len(rows) == 1 and rows[0].label == "animal/dog"
    assert "animal/dog" in dict(out.agg.categories)
    assert out.vision is not None and out.vision.top[0][0] == "dog"
    # basic depth never uses it
    basic = run_basic(_inp(_result("dog", 0.95)))
    assert not [e for e in basic.evidence if e.kind == "vision"] and basic.vision is None


def test_logo_guess_counts_when_the_words_agree_not_when_they_disagree() -> None:
    # the name is about a dog too: the logo is a second, independent witness
    agree = run_full(_inp(_result("dog", 0.95), name="Shiba Puppy", symbol="PUP"))
    alone = run_full(_inp(None, name="Shiba Puppy", symbol="PUP"))
    assert any(e.kind == "vision" for e in agree.evidence)
    assert dict(agree.agg.categories)["animal/dog"] > dict(alone.agg.categories)["animal/dog"]
    # the name is about Trump: a dog in the logo is the mascot, not the subject
    other = run_full(_inp(_result("dog", 0.95), name="Trump Victory", symbol="TRUMPV"))
    assert not any(e.kind == "vision" for e in other.evidence)
    assert "animal/dog" not in dict(other.agg.categories)


def test_gate_keeps_rows_for_coins_with_only_context_labels() -> None:
    (ev,) = vision.evidence(_result("dog", 0.95))
    assert vision.gate([ev], [("derivative/copycat", 0.7)]) == [ev]
    assert vision.gate([ev], [("animal/other", 0.4)]) == [ev]
    assert vision.gate([ev], [("political", 0.6)]) == []


def test_logo_guess_alone_stays_under_the_main_filter() -> None:
    out = run_full(_inp(_result("dog", 1.0)))
    assert dict(out.agg.categories)["animal/dog"] < 0.5


# ----------------------------------------------------------------- analyzer, cached per logo


@pytest.fixture
def clip_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)
    settings.enable_clip = True
    settings.vision_model_path = "/stub"
    return settings


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await conn.execute("truncate token_metadata, image, lookup_cache cascade")
    try:
        yield conn
    finally:
        await conn.close()


@needs_db
async def test_full_depth_labels_the_logo_once(
    clip_settings: Settings,
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enc = StubEncoder("dog")
    monkeypatch.setattr(vision, "default_encoder", lambda s: enc if s.enable_clip else None)
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Barkley", "BARK", f"https://ipfs.io/ipfs/{CID_META}")
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10")
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.depth == "full"
    assert a.image.labels and a.image.labels[0].label == "dog"
    assert a.image.labels[0].model == vision.load_config().model
    assert any(e.kind == "vision" and e.label == "animal/dog" for e in a.evidence)
    assert enc.calls == 1
    stored = await db.fetchval("select labels from image where content_key=$1", f"ipfs:{CID_IMG}")
    assert json.loads(stored)["top"][0][0] == "dog"  # jsonb object (no codec on this conn)

    # again: the labels come from the image row, the model is not run a second time
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        r2 = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&refresh=true&wait=10")
    a2 = TokenResponse.model_validate(r2.json()).analysis
    assert a2 is not None and a2.image.labels and a2.image.labels[0].label == "dog"
    assert enc.calls == 1


# ----------------------------------------------------------------- audit fixes (rules 0.26.0)


def test_a_name_whose_only_topic_is_its_script_still_takes_the_logo_guess() -> None:
    (ev,) = vision.evidence(_result("cat", 0.95))
    assert vision.gate([ev], [("regional_language", 0.6)]) == [ev]
    out = run_full(_inp(_result("cat", 0.95), name="ネコ", symbol="NEKO"))
    assert any(e.kind == "vision" for e in out.evidence)


def test_16_bit_greyscale_is_not_read_as_a_white_square() -> None:
    arr = (np.arange(64 * 64).reshape(64, 64) * 12 + 1000).astype(np.uint16)  # all above 255
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    px = vision.preprocess(buf.getvalue())
    assert px.min() < -0.9 and px.max() > 0.9  # the full range, not clipped to white


def test_an_unreadable_image_is_cached_but_a_model_failure_is_not() -> None:
    bad = vision.label_image(StubEncoder("dog"), b"not an image")
    assert bad.error and not bad.error.startswith(vision.MODEL_ERROR)
    back = vision.from_json(json.loads(json.dumps(vision.to_json(bad))))
    assert back is not None and back.top == [] and back.error

    class Broken:
        def encode(self, pixels: np.ndarray) -> np.ndarray:
            raise RuntimeError("onnxruntime fell over")

    failed = vision.label_image(Broken(), _png())
    assert (failed.error or "").startswith(vision.MODEL_ERROR)


def test_flag_on_without_a_model_path_logs_once(settings: Settings) -> None:
    settings.enable_clip = True
    settings.vision_model_path = ""
    assert vision.default_encoder(settings) is None
    assert "VISION_MODEL_PATH" in (vision.load_error() or "")


@needs_db
async def test_a_logo_that_never_downloaded_is_not_fetched_again_for_labels(
    clip_settings: Settings,
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enc = StubEncoder("dog")
    monkeypatch.setattr(vision, "default_encoder", lambda s: enc if s.enable_clip else None)
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Barkley", "BARK", f"https://ipfs.io/ipfs/{CID_META}")
    gets: list[str] = []

    def img(request: httpx.Request) -> httpx.Response:
        gets.append(str(request.url))
        return httpx.Response(503)

    for refresh in ("", "&refresh=true"):
        gets.clear()
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=rf".*/ipfs/{CID_IMG}.*").mock(side_effect=img)
            install_web(router, chain)
            r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=20{refresh}")
        assert r.status_code == 200, r.text
        a = TokenResponse.model_validate(r.json()).analysis
        assert a is not None and a.image.labels == []
    assert enc.calls == 0
    assert gets == []  # the second read: metadata is cached and vision does not refetch
