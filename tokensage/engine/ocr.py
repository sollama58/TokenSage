"""OCR on the token image (full depth). RapidOCR (PP-OCR in ONNX, Apache-2.0), loaded lazily
because it costs ~250 MB RSS. Never raises."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

_lock = threading.Lock()
_engine = None
_failed: str | None = None
MAX_SIDE = 640
MIN_CONF = 0.6


@dataclass
class OcrLine:
    text: str
    confidence: float


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _get_engine():  # noqa: ANN202
    global _engine, _failed
    if _engine is not None or _failed:
        return _engine
    with _lock:
        if _engine is None and not _failed:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
            except Exception as e:  # noqa: BLE001
                _failed = f"{type(e).__name__}: {e}"
    return _engine


def read(data: bytes) -> tuple[list[OcrLine], str | None]:
    """Return (lines, error). Lines are confidence-filtered and de-duplicated."""
    eng = _get_engine()
    if eng is None:
        return [], _failed or "ocr unavailable"
    try:
        with Image.open(io.BytesIO(data)) as img:
            try:
                img.seek(0)
            except Exception:  # noqa: BLE001
                pass
            rgb = img.convert("RGB")
            rgb.thumbnail((MAX_SIDE, MAX_SIDE))
            arr = np.asarray(rgb)
        result, _elapsed = eng(arr)
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}"[:160]
    lines: list[OcrLine] = []
    seen: set[str] = set()
    for item in result or []:
        try:
            _box, text, conf = item
        except (TypeError, ValueError):
            continue
        t = str(text).strip()
        if len(t) < 2 or float(conf) < MIN_CONF:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(OcrLine(t, round(float(conf), 3)))
    return lines[:12], None
