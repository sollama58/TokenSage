"""Full audit 2026-11-02 (TrenchScanner project thread), TS-2: a read that failed on a limited
or failing RPC is retried later each time, not three times inside the same storm."""

from __future__ import annotations

from tokensage import worker
from tokensage.resolve.rpc import RpcError


def test_rpc_limit_failures_retry_later_each_time() -> None:
    limited = RpcError("rpc rate limited (429)", http_status=429)
    assert [worker.rpc_retry_delay_s(limited, n) for n in (1, 2, 3, 4)] == [30, 120, 480, 600]
    assert worker.rpc_retry_delay_s(RpcError("rpc http 503", http_status=503), 2) == 120
    # Wrapped by the resolver: still the RPC's failure.
    try:
        raise RuntimeError("resolve failed") from limited
    except RuntimeError as wrapped:
        assert worker.rpc_retry_delay_s(wrapped, 1) == 30
    # Anything else keeps the queue's own delay.
    bad = RpcError("rpc http 400: bad", retryable=False, http_status=400)
    assert worker.rpc_retry_delay_s(bad, 1) is None
    assert worker.rpc_retry_delay_s(ValueError("odd json"), 1) is None
