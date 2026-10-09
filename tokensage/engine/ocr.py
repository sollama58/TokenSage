"""OCR on the token image (full depth). RapidOCR (PP-OCR in ONNX, Apache-2.0), loaded lazily
because it costs ~250 MB RSS. Never raises."""

from __future__ import annotations

import asyncio
import io
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

_lock = threading.Lock()
_engine = None
_failed: str | None = None
MAX_SIDE = 640
MIN_CONF = 0.6
# A strip thinner than MIN_SIDE px, or longer than MAX_ASPECT times its height, after the
# scaling to MAX_SIDE (a 10000x10 logo becomes 640x1) is not read: PP-OCR skips detection on
# it and resizes the whole strip to 48 px high for the recogniser, tens of thousands of px
# wide, which takes minutes and gigabytes.
MIN_SIDE = 16
MAX_ASPECT = 40
# A second layer for shapes the guard does not catch: the engine runs on its own thread and
# the read gives up after this many seconds (the engine thread finishes on its own).
TIME_BUDGET_S = 30.0


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


_slots = threading.BoundedSemaphore(1)
_concurrency = 1
_async_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def set_concurrency(n: int) -> None:
    """How many OCR reads may run at once (engine runs happen on worker threads)."""
    global _slots, _concurrency, _async_slots
    _concurrency = max(1, n)
    _slots = threading.BoundedSemaphore(_concurrency)
    _async_slots = None


def _async_semaphore() -> asyncio.Semaphore:
    """The same cap for async callers, one per event loop (a semaphore is bound to its loop)."""
    global _async_slots
    loop = asyncio.get_running_loop()
    if _async_slots is None or _async_slots[0] is not loop:
        _async_slots = (loop, asyncio.Semaphore(_concurrency))
    return _async_slots[1]


async def read_async(data: bytes) -> tuple[list[OcrLine], str | None]:
    """read() for async callers: waits for an OCR slot on the event loop, so a job queued
    behind another OCR does not hold an executor thread (which other jobs' hashing and
    engine runs need) while it waits."""
    async with _async_semaphore():
        return await asyncio.to_thread(read, data)


def read(data: bytes) -> tuple[list[OcrLine], str | None]:
    """Return (lines, error). Lines are confidence-filtered and de-duplicated."""
    with _slots:
        return _read(data)


def _run_with_budget(eng: Any, arr: np.ndarray) -> Any:
    """The engine's result, or TimeoutError after TIME_BUDGET_S: a pathological input must
    not hold the OCR slot (and the job) for minutes."""
    box: list[Any] = []

    def run() -> None:
        try:
            box.append(eng(arr)[0])
        except Exception as e:  # noqa: BLE001
            box.append(e)

    t = threading.Thread(target=run, name="ocr-engine", daemon=True)
    t.start()
    t.join(TIME_BUDGET_S)
    if t.is_alive():
        raise TimeoutError(f"OCR gave up after {TIME_BUDGET_S:.0f} s")
    if isinstance(box[0], Exception):
        raise box[0]
    return box[0]


def _read(data: bytes) -> tuple[list[OcrLine], str | None]:
    eng = _get_engine()
    if eng is None:
        return [], _failed or "ocr unavailable"
    try:
        with Image.open(io.BytesIO(data)) as img:
            from tokensage.engine.image import MAX_PIXELS, to_rgb

            if img.size[0] * img.size[1] > MAX_PIXELS:
                return [], "image too large"
            try:
                img.seek(0)
            except Exception:  # noqa: BLE001
                pass
            arr = np.asarray(to_rgb(img, MAX_SIDE))
        h, w = int(arr.shape[0]), int(arr.shape[1])
        if min(h, w) < MIN_SIDE or max(h, w) > MAX_ASPECT * max(1, min(h, w)):
            return [], f"image too thin for OCR ({w}x{h} after scaling)"
        result = _run_with_budget(eng, arr)
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
