"""S6 Image stage, basic depth (guide §5.6): safe decode, perceptual hashes (incl. mirror),
palette, animation, and near-duplicate matching against candidate hashes. No OCR/CLIP here."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime

import imagehash
import numpy as np
from PIL import Image, ImageFile

# 16 Mpx (4000x4000, ~64 MB as RGBA): a logo is a few hundred pixels a side, and one read
# decodes it up to three times (hashes, OCR, vision) with 8 jobs to a 2 GB worker; at 40 Mpx
# a few hostile PNGs were an out-of-memory route (audit 2026-10-14 M9)
MAX_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = True

# The only decoders attacker bytes reach (logos, X media, CoinGecko images): Pillow otherwise
# tries every plugin it has (PSD, TGA, SGI, EPS...) on whatever a URL returns.
SAFE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "AVIF")

# GIF limits for the header walk below (audit 2026-10-23 SD-1). A logo GIF has a few hundred
# frames at most and no real comment; Pillow joins comment sub-blocks quadratically
# (GHSA-74r2-7ff9-mjj2: a 5 MB GIF of comments cost 4 s per open) and n_frames walks them all.
GIF_MAX_FRAMES = 2000
GIF_MAX_COMMENT_BYTES = 64 * 1024
GIF_MAX_BLOCKS = 200_000  # sub-blocks walked; a 5 MB GIF of full 255-byte blocks is ~20k

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _too_large(w: int, h: int) -> bool:
    return w * h > MAX_PIXELS


def _gif_check(data: bytes) -> None:
    """Walk the GIF block structure (linear, no decoding): every frame inside MAX_PIXELS, at
    most GIF_MAX_FRAMES frames, GIF_MAX_COMMENT_BYTES of comments, GIF_MAX_BLOCKS blocks."""
    n = len(data)
    if n < 13:
        raise ValueError("truncated gif header")
    if _too_large(int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")):
        raise ValueError("image too large")
    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 << ((flags & 7) + 1)
    frames = comment = blocks = 0

    def skip_sub_blocks(p: int, count_comment: bool) -> int:
        nonlocal comment, blocks
        while p < n:
            size = data[p]
            p += 1
            if size == 0:
                return p
            blocks += 1
            if blocks > GIF_MAX_BLOCKS:
                raise ValueError("gif has too many blocks")
            if count_comment:
                comment += size
                if comment > GIF_MAX_COMMENT_BYTES:
                    raise ValueError("gif comment too large")
            p += size
        return p

    while pos < n:
        tag = data[pos]
        if tag == 0x3B:  # trailer
            break
        if tag == 0x21:  # extension
            if pos + 1 >= n:
                break
            label = data[pos + 1]
            pos = skip_sub_blocks(pos + 2, count_comment=label == 0xFE)
        elif tag == 0x2C:  # image descriptor
            if pos + 10 > n:
                break
            # Pillow grows the canvas to a frame that reaches past the screen: offset + size
            fx = int.from_bytes(data[pos + 1 : pos + 3], "little")
            fy = int.from_bytes(data[pos + 3 : pos + 5], "little")
            fw = int.from_bytes(data[pos + 5 : pos + 7], "little")
            fh = int.from_bytes(data[pos + 7 : pos + 9], "little")
            if _too_large(fx + fw, fy + fh):
                raise ValueError("image too large")
            frames += 1
            if frames > GIF_MAX_FRAMES:
                raise ValueError("gif has too many frames")
            fflags = data[pos + 9]
            pos += 10
            if fflags & 0x80:
                pos += 3 << ((fflags & 7) + 1)
            pos = skip_sub_blocks(pos + 1, count_comment=False)  # +1: LZW minimum code size
        else:
            break  # garbage: Pillow stops here too


def precheck(data: bytes) -> None:
    """Refuse oversized images from their headers alone, before Pillow allocates anything.

    Pillow 12.3 fills an APNG's first frame at its IHDR size before Image.open's own
    MAX_IMAGE_PIXELS check (GHSA-5xm2-6676-qcp9: a 126-byte file allocated ~1 GB), so the
    check after open comes too late. PNG (IHDR), GIF (screen and every frame) and WebP
    (VP8X canvas, VP8/VP8L frame) are checked here; JPEG and AVIF are checked from the size
    Image.open reads lazily, which allocates no pixels. Raises ValueError."""
    if data[:8] == _PNG_SIG:
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValueError("png without IHDR")
        if _too_large(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")):
            raise ValueError("image too large")
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        _gif_check(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
        elif chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 ":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
        else:
            return
        if _too_large(w, h):
            raise ValueError("image too large")


def open_checked(data: bytes) -> Image.Image:
    """Image.open on the safe decoders, after precheck, with the lazily read size checked
    too (no pixels are decoded yet). The caller closes it (use as a context manager)."""
    precheck(data)
    img = Image.open(io.BytesIO(data), formats=SAFE_FORMATS)
    w, h = img.size
    if w * h > MAX_PIXELS:
        img.close()
        raise ValueError("image too large")
    return img


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
    created_at: datetime | None = None  # when the token behind a mint candidate launched
    name: str | None = None  # the token behind a mint candidate (lineage, copy_of)
    symbol: str | None = None


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
        # scaled down before the float conversion: a 6300x6300 16-bit PNG is 80 KB on the
        # wire but 317 MB as a float64 frame (reduce() and thumbnail() reject I;16, so a
        # nearest-neighbour resize)
        if max(img.size) > 2 * max_side:
            k = max(img.size) // (2 * max_side)
            img = img.resize(
                (max(1, img.size[0] // k), max(1, img.size[1] // k)), Image.Resampling.NEAREST
            )
        arr = np.asarray(img, dtype=np.float32)
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
    with open_checked(data) as img:
        w, h = img.size
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


def all_near(f: ImageFeatures, candidates: list[Candidate], max_distance: int) -> list[NearDup]:
    """Every candidate within max_distance (plain or mirrored), nearest first."""
    out: list[NearDup] = []
    for c in candidates:
        d = hamming(f.phash, c.phash)
        dm = hamming(f.phash_mirror, c.phash)
        if min(d, dm) <= max_distance:
            out.append(NearDup(c, min(d, dm), mirrored=dm < d))
    out.sort(key=lambda n: n.distance)
    return out


def near_duplicates(
    f: ImageFeatures, candidates: list[Candidate], max_distance: int
) -> list[NearDup]:
    return all_near(f, candidates, max_distance)[:10]


def analyze(data: bytes | None, candidates: list[Candidate], max_distance: int) -> ImageResult:
    if not data:
        return ImageResult(features=None, error="no image bytes")
    try:
        f = features(data)
    except Exception as e:  # noqa: BLE001 - hostile images must not kill the pipeline
        return ImageResult(features=None, error=f"decode failed: {type(e).__name__}: {e}"[:200])
    return ImageResult(features=f, near=near_duplicates(f, candidates, max_distance))
