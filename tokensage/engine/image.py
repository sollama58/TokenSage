"""S6 Image stage, basic depth (guide §5.6): safe decode, perceptual hashes (incl. mirror),
palette, animation, and near-duplicate matching against candidate hashes. No OCR/CLIP here."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import imagehash
import numpy as np
from PIL import Image, ImageFile

MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = True

_CSS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "gray": (128, 128, 128),
    "silver": (192, 192, 192),
    "red": (220, 40, 40),
    "orange": (255, 140, 0),
    "yellow": (250, 220, 40),
    "green": (40, 160, 60),
    "lime": (120, 220, 60),
    "teal": (0, 128, 128),
    "cyan": (0, 200, 220),
    "blue": (40, 90, 220),
    "navy": (20, 30, 90),
    "purple": (128, 0, 160),
    "pink": (255, 120, 180),
    "magenta": (220, 40, 200),
    "brown": (130, 80, 40),
    "tan": (210, 180, 140),
    "beige": (235, 220, 190),
    "olive": (128, 128, 0),
    "gold": (212, 175, 55),
    "maroon": (128, 0, 0),
}


@dataclass
class ImageFeatures:
    phash: int
    dhash: int
    phash_mirror: int
    width: int
    height: int
    animated: bool
    frames: int
    palette_hex: list[str]
    palette_names: list[str]
    format: str | None


@dataclass
class Candidate:
    content_key: str
    phash: int
    mint: str | None = None
    known_coin: str | None = None
    template: str | None = None


@dataclass
class NearDup:
    candidate: Candidate
    distance: int
    mirrored: bool = False


@dataclass
class ImageResult:
    features: ImageFeatures | None
    near: list[NearDup] = field(default_factory=list)
    error: str | None = None


def _to_signed64(h: imagehash.ImageHash) -> int:
    v = int(str(h), 16)
    return v - (1 << 64) if v >= 1 << 63 else v


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def _nearest_name(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    best, bd = "gray", 10**9
    for name, (cr, cg, cb) in _CSS.items():
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < bd:
            best, bd = name, d
    return best


def to_rgb(img: Image.Image, max_side: int = 512) -> Image.Image:
    """RGB at most max_side on a side, decoding large images at reduced size where the
    format allows it (JPEG draft), and mapping 16-bit/float greyscale to 8 bit first
    (a plain convert("RGB") clips those to near-white, so all hash alike)."""
    if img.format == "JPEG":
        img.draft("RGB", (max_side, max_side))
    if img.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
        arr = np.asarray(img, dtype=np.float64)
        lo, hi = float(arr.min()), float(arr.max())
        scaled = (arr - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(arr)
        img = Image.fromarray(scaled.astype(np.uint8))  # 2-D uint8 -> mode "L"
    elif max(img.size) > 2 * max_side:
        img = img.reduce(max(1, max(img.size) // (2 * max_side)))
    out = img.convert("RGB")
    out.thumbnail((max_side, max_side))
    return out


def _frame(img: Image.Image, index: int) -> Image.Image:
    try:
        img.seek(index)
    except EOFError:
        img.seek(0)
    return to_rgb(img)


def features(data: bytes) -> ImageFeatures:
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ValueError("image too large")
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        animated = n_frames > 1
        fmt = img.format
        base = _frame(img, 0)
        ph = imagehash.phash(base)
        dh = imagehash.dhash(base)
        mirror = imagehash.phash(base.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
        small = base.resize((64, 64))
        q = small.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)
        hexes: list[str] = []
        names: list[str] = []
        for _cnt, idx_obj in counts[:5]:
            idx = int(idx_obj)  # type: ignore[call-overload]
            chunk = [int(v) for v in pal[idx * 3 : idx * 3 + 3]]
            if len(chunk) == 3:
                rgb = (chunk[0], chunk[1], chunk[2])
                hexes.append(f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
                nm = _nearest_name(rgb)
                if nm not in names:
                    names.append(nm)
        return ImageFeatures(
            phash=_to_signed64(ph),
            dhash=_to_signed64(dh),
            phash_mirror=_to_signed64(mirror),
            width=w,
            height=h,
            animated=animated,
            frames=n_frames,
            palette_hex=hexes,
            palette_names=names,
            format=fmt,
        )


def near_duplicates(
    f: ImageFeatures, candidates: list[Candidate], max_distance: int
) -> list[NearDup]:
    out: list[NearDup] = []
    for c in candidates:
        d = hamming(f.phash, c.phash)
        dm = hamming(f.phash_mirror, c.phash)
        if min(d, dm) <= max_distance:
            out.append(NearDup(c, min(d, dm), mirrored=dm < d))
    out.sort(key=lambda n: n.distance)
    return out[:10]


def analyze(data: bytes | None, candidates: list[Candidate], max_distance: int) -> ImageResult:
    if not data:
        return ImageResult(features=None, error="no image bytes")
    try:
        f = features(data)
    except Exception as e:  # noqa: BLE001 - hostile images must not kill the pipeline
        return ImageResult(features=None, error=f"decode failed: {type(e).__name__}: {e}"[:200])
    return ImageResult(features=f, near=near_duplicates(f, candidates, max_distance))
