"""A thread pool of its own for CPU-bound work (the engine passes, image decoding and hashing,
OCR, logo labels, matcher builds).

asyncio.to_thread runs on the event loop's default executor, which is also where every DNS
lookup of every fetch goes (loop.getaddrinfo). With CPU work filling those threads, each of the
analyzer's concurrent jobs waited on DNS behind another job's engine pass. Keeping CPU work here
leaves the default executor to the network.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_executor: ThreadPoolExecutor | None = None
_threads = 3


def set_threads(n: int) -> None:
    """Size of the pool (CPU_THREADS); takes effect for a pool not yet started."""
    global _threads
    _threads = max(1, n)


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_threads, thread_name_prefix="cpu")
    return _executor


async def run[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """asyncio.to_thread, on the CPU pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool(), functools.partial(fn, *args, **kwargs))
