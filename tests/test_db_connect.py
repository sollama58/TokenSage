"""create_pool waits for a database that is still starting, then gives up after wait_s."""

from __future__ import annotations

import socket
import time

import pytest

from tokensage import db


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now: connection refused, like a starting database
    return int(port)


async def test_create_pool_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "CONNECT_RETRY_S", 0.2)
    dsn = f"postgresql://x@127.0.0.1:{_closed_port()}/x"
    t0 = time.monotonic()
    with pytest.raises(OSError):
        await db.create_pool(dsn, wait_s=1.0)
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed < 5.0, elapsed  # it retried for the window instead of failing at once


async def test_create_pool_no_wait_fails_fast() -> None:
    dsn = f"postgresql://x@127.0.0.1:{_closed_port()}/x"
    t0 = time.monotonic()
    with pytest.raises(OSError):
        await db.create_pool(dsn, wait_s=0)
    assert time.monotonic() - t0 < 1.0
