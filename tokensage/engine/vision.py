"""Optional logo labels from a local vision model (flag ENABLE_CLIP, guide §5.6).

A SigLIP image tower in ONNX (8-bit, ~100 MB, Apache-2.0) turns the logo into one vector.
A small head (data/vision_head.npz, built by scripts/build_vision_head.py from hand-labelled
pump.fun logos) scores it against ~25 visual classes: half from a linear probe fitted on those
logos, half zero-shot from precomputed label-prompt embeddings, so no text model ships.
The top class becomes a "vision guess" evidence row on the image input when it maps to a
taxonomy label and clears that class's cutoff (tuned for ~90% precision on held-out logos),
and counts when the name, ticker or description agree with it or found no topic (gate()).
Rows weigh at most 0.4 and never name a referent. Local inference only (like OCR), so it is
within the no-external-AI rule.

The engine works fully without it: with the flag off nothing here is loaded, and
onnxruntime is imported only when the model is first needed. Never raises.
"""

from __future__ import annotations

import asyncio
import io
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import structlog
import yaml

from tokensage.engine.context import Ev
from tokensage.taxonomy import DATA_DIR, category_labels

if TYPE_CHECKING:
    from tokensage.config import Settings

MAX_WEIGHT = 0.4  # hard ceiling, whatever the yaml says
SIZE = 224
MAX_PIXELS = 40_000_000
TOP_K = 3  # classes kept in the result (and cached), best first
MODEL_ERROR = "model: "  # prefix of an error the model raised (worth retrying, never cached)


class Encoder(Protocol):
    """Anything that turns preprocessed (1, 3, 224, 224) pixels into one image vector."""

    def encode(self, pixels: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True)
class VisionConfig:
    model: str  # short id reported with every label, e.g. "siglip-b16-q8/1"
    classes: dict[str, str | None]  # visual class -> taxonomy label (None: never emitted)
    names: dict[str, str]  # visual class -> words for the evidence detail
    thresholds: dict[str, float]  # emitted classes only; a class without one never emits
    zero_shot_share: float
    zero_shot_scale: float
    probe_scale: float
    weight_floor: float
    max_weight: float
    min_margin: float
    context_labels: tuple[str, ...] = ()


@lru_cache
def load_config() -> VisionConfig:
    with (DATA_DIR / "vision_labels.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    known = category_labels()
    classes = {
        str(c): (str(v["label"]) if v.get("label") else None) for c, v in raw["classes"].items()
    }
    unknown = sorted({v for v in classes.values() if v} - known)
    assert not unknown, f"vision_labels.yaml names labels not in taxonomy.yaml: {unknown}"
    s = raw["scoring"]
    return VisionConfig(
        model=str(raw["model"]),
        classes=classes,
        names={str(c): str(v.get("name") or c) for c, v in raw["classes"].items()},
        thresholds={str(c): float(t) for c, t in (raw.get("thresholds") or {}).items()},
        zero_shot_share=float(s["zero_shot_share"]),
        zero_shot_scale=float(s["zero_shot_scale"]),
        probe_scale=float(s["probe_scale"]),
        weight_floor=float(s["weight_floor"]),
        max_weight=min(MAX_WEIGHT, float(s["max_weight"])),
        min_margin=float(s.get("min_margin", 0.0)),
        context_labels=tuple(raw.get("context_labels", [])),
    )


@dataclass(frozen=True)
class Head:
    classes: list[str]
    centroids: np.ndarray  # (K, D) unit prompt-centroid per class
    w: np.ndarray  # (D, K) linear probe
    b: np.ndarray  # (K,)


@lru_cache
def load_head() -> Head:
    with np.load(DATA_DIR / "vision_head.npz", allow_pickle=False) as z:
        return Head(
            classes=[str(c) for c in z["classes"]],
            centroids=np.asarray(z["centroids"], np.float32),
            w=np.asarray(z["w"], np.float32),
            b=np.asarray(z["b"], np.float32),
        )


# ----------------------------------------------------------------- scoring
@dataclass
class VisionResult:
    model: str
    top: list[tuple[str, float]] = field(default_factory=list)  # (class, score), best first
    error: str | None = None


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def scores(vec: np.ndarray, head: Head, cfg: VisionConfig) -> np.ndarray:
    """Per-class score in 0-1 (they sum to 1): the probe and the zero-shot prompts blended."""
    v = np.asarray(vec, np.float32).reshape(-1)
    v = v / max(float(np.linalg.norm(v)), 1e-9)
    zs = _softmax(cfg.zero_shot_scale * (head.centroids @ v))
    pr = _softmax((cfg.probe_scale * v) @ head.w + head.b)
    a = cfg.zero_shot_share
    return (1 - a) * pr + a * zs


def classify(
    vec: np.ndarray, head: Head | None = None, cfg: VisionConfig | None = None
) -> VisionResult:
    cfg = cfg or load_config()
    head = head or load_head()
    s = scores(vec, head, cfg)
    order = np.argsort(-s)[:TOP_K]
    return VisionResult(cfg.model, [(head.classes[i], round(float(s[i]), 3)) for i in order])


def weight(score: float, threshold: float, cfg: VisionConfig) -> float:
    frac = max(0.0, min(1.0, (score - threshold) / max(1e-6, 1.0 - threshold)))
    return round(
        min(cfg.max_weight, cfg.weight_floor + (cfg.max_weight - cfg.weight_floor) * frac), 3
    )


def evidence(res: VisionResult | None, cfg: VisionConfig | None = None) -> list[Ev]:
    """At most one row: the top class, when it maps to a taxonomy label, clears its cutoff and
    beats the runner-up by the margin. Results from another model version are ignored."""
    if res is None or not res.top:
        return []
    cfg = cfg or load_config()
    if res.model != cfg.model:
        return []
    cls, score = res.top[0]
    label = cfg.classes.get(cls)
    thr = cfg.thresholds.get(cls)
    if label is None or thr is None or score < thr:
        return []
    runner = res.top[1][1] if len(res.top) > 1 else 0.0
    if score - runner < cfg.min_margin:
        return []
    return [
        Ev(
            kind="vision",
            label=label,
            weight=weight(score, thr, cfg),
            detail=f"vision guess: the logo looks like {cfg.names.get(cls, cls)} "
            f"(score {score:.2f})",
            source=f"vision:{cls}",
            where="image",
        )
    ]


def gate(
    evs: list[Ev], category_scores: list[tuple[str, float]], cfg: VisionConfig | None = None
) -> list[Ev]:
    """The rows that may count: a logo guess agrees with a topic the words already found
    (same top-level category), or the words found no topic at all. A dog drawn on a coin
    that is about something else is the mascot, not the subject."""
    cfg = cfg or load_config()
    tops = {lbl.split("/")[0] for lbl, _ in category_scores}
    topical = [
        lbl
        for lbl, _ in category_scores
        if not any(lbl == c or lbl.startswith(c + "/") for c in cfg.context_labels)
    ]
    return [ev for ev in evs if not topical or ev.label.split("/")[0] in tops]


# ----------------------------------------------------------------- image -> pixels
_MEAN = np.array([0.5, 0.5, 0.5], np.float32)  # SigLIP normalisation
_STD = np.array([0.5, 0.5, 0.5], np.float32)


def preprocess(data: bytes) -> np.ndarray:
    """First frame, transparency on white, squashed to 224x224 (as SigLIP was trained)."""
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ValueError("image too large")
        if img.format == "JPEG":
            img.draft("RGB", (SIZE * 2, SIZE * 2))
        img.seek(0)
        if max(img.size) > 4 * SIZE:
            img.thumbnail((4 * SIZE, 4 * SIZE))
        if img.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
            # convert() clips 16-bit/float greyscale to near-white: scale to 8 bit as the
            # image stage does (image.to_rgb)
            from tokensage.engine.image import to_rgb

            rgba = to_rgb(img, max_side=4 * SIZE).convert("RGBA")
        else:
            rgba = img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    bg.alpha_composite(rgba)
    rgb = bg.convert("RGB").resize((SIZE, SIZE), Image.Resampling.BICUBIC)
    a = (np.asarray(rgb, np.float32) / 255.0 - _MEAN) / _STD
    return a.transpose(2, 0, 1)[None]


class OnnxEncoder:
    def __init__(self, model_path: Path, threads: int = 1) -> None:
        import onnxruntime as ort  # only reached with ENABLE_CLIP on

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), opts, providers=["CPUExecutionProvider"]
        )
        outs = [o.name for o in self.session.get_outputs()]
        # SigLIP exports name the pooled vector pooler_output; CLIP exports image_embeds
        self.output = next((o for o in ("image_embeds", "pooler_output") if o in outs), outs[-1])

    def encode(self, pixels: np.ndarray) -> np.ndarray:
        (out,) = self.session.run([self.output], {"pixel_values": pixels})
        return np.asarray(out, np.float32)[0]


def label_image(encoder: Encoder, data: bytes) -> VisionResult:
    """Labels for one image. Never raises: a bad image or model error is in .error."""
    cfg = load_config()
    try:
        pixels = preprocess(data)
    except Exception as e:  # noqa: BLE001 - hostile images must not kill the analysis
        return VisionResult(cfg.model, error=f"image: {type(e).__name__}: {e}"[:200])
    try:
        return classify(encoder.encode(pixels), cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return VisionResult(cfg.model, error=f"{MODEL_ERROR}{type(e).__name__}: {e}"[:200])


# ----------------------------------------------------------------- process-wide model
_enc_lock = threading.Lock()
_encoder: Encoder | None = None
_load_error: str | None = None
_loaded_from: str | None = None


def resolve_path(model_path: str) -> Path:
    """VISION_MODEL_PATH may be the .onnx file or a directory holding vision_model*.onnx
    (or onnx/vision_model*.onnx, as Hugging Face exports lay it out)."""
    p = Path(model_path)
    if not p.is_dir():
        return p
    for c in (
        p / "vision_model_quantized.onnx",
        p / "vision_model.onnx",
        p / "onnx" / "vision_model_quantized.onnx",
        p / "onnx" / "vision_model.onnx",
    ):
        if c.is_file():
            return c
    return p / "vision_model_quantized.onnx"


def default_encoder(settings: Settings) -> Encoder | None:
    """The process-wide encoder, or None when the flag is off or the model is unusable.
    Loaded once, lazily; a failed load is remembered (and logged) instead of retried."""
    global _encoder, _load_error, _loaded_from
    if not settings.enable_clip:
        return None
    if not settings.vision_model_path:
        if _loaded_from != "":
            _loaded_from, _encoder = "", None
            _load_error = "ENABLE_CLIP is on but VISION_MODEL_PATH is not set"
            structlog.get_logger("vision").warning("vision_model_unavailable", error=_load_error)
        return None
    key = f"{settings.vision_model_path}|{settings.vision_threads}"
    if _loaded_from == key:
        return _encoder
    with _enc_lock:
        if _loaded_from != key:
            _encoder, _load_error = None, None
            path = resolve_path(settings.vision_model_path)
            try:
                if not path.is_file():
                    raise FileNotFoundError(f"no ONNX model at {path}")
                load_config()
                load_head()
                _encoder = OnnxEncoder(path, settings.vision_threads)
            except Exception as e:  # noqa: BLE001
                _load_error = f"{type(e).__name__}: {e}"
                structlog.get_logger("vision").warning(
                    "vision_model_unavailable", error=_load_error
                )
            _loaded_from = key
    return _encoder


def load_error() -> str | None:
    return _load_error


_async_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


async def label_async(encoder: Encoder, data: bytes) -> VisionResult:
    """label_image() for async callers: one image at a time per process, waited for on the
    event loop so a queued job does not hold an executor thread."""
    global _async_slots
    loop = asyncio.get_running_loop()
    if _async_slots is None or _async_slots[0] is not loop:
        _async_slots = (loop, asyncio.Semaphore(1))
    async with _async_slots[1]:
        return await asyncio.to_thread(label_image, encoder, data)


def to_json(res: VisionResult) -> dict[str, Any]:
    out: dict[str, Any] = {"model": res.model, "top": [[c, s] for c, s in res.top]}
    if res.error:
        out["error"] = res.error
    return out


def from_json(raw: Any) -> VisionResult | None:
    """A cached result (image.labels), or None when absent or unreadable. An image the model
    could not read is cached too (no labels, .error set), so it is not retried every read."""
    try:
        if not isinstance(raw, dict) or not (raw.get("top") or raw.get("error")):
            return None
        return VisionResult(
            str(raw["model"]),
            [(str(c), float(s)) for c, s in raw.get("top") or []],
            error=str(raw["error"])[:200] if raw.get("error") else None,
        )
    except Exception:  # noqa: BLE001
        return None
