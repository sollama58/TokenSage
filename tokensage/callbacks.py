"""Signed webhook callbacks for batch jobs (guide §6.4, optional).

When a batch request carries `callback_url`, each finished job enqueues a `callback` job that
POSTs the JobResponse JSON to that URL. The body is signed with HMAC-SHA256 using, as the key,
the SHA-256 hex digest of the caller's API key (so the consumer can verify with nothing but
the key it already has). Delivery goes through the SSRF guard and is retried by the queue.

Headers:  X-TokenSage-Signature: sha256=<hex>
          X-TokenSage-Timestamp: <unix seconds>
          X-TokenSage-Job: <job id>
Signed string: f"{timestamp}.{body}"
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import structlog

from tokensage import queue
from tokensage.net.safe_fetch import UnsafeUrl, check_url

log = structlog.get_logger("callbacks")
CALLBACK_RETRY_S = 30
CALLBACK_MAX_ATTEMPTS = 3
# A callback waits for its target job; finishing the target releases it at once (see
# queue._release_callbacks). This is only the fallback re-check interval.
CALLBACK_WAIT_S = 60
# The whole delivery (connect, send, response headers, the few body bytes read) must finish
# within this: httpx's own timeout is per socket operation, so a consumer endpoint that
# trickles its response could otherwise hold a worker slot forever.
CALLBACK_TOTAL_TIMEOUT_S = 15.0
CALLBACK_BODY_MAX_BYTES = 4096  # only the status matters; never buffer a large body


def sign(secret_hex: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret_hex.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret_hex: str, timestamp: int, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign(secret_hex, timestamp, body), signature)


async def validate_callback_url(url: str) -> str:
    """https + public host, checked at submit time (and again at delivery)."""
    return await check_url(url)


async def schedule(
    conn: asyncpg.Connection, *, target_job_id: int, callback_url: str, key_digest: str
) -> None:
    await conn.execute(
        """insert into job (kind, mint, depth, priority, payload, run_after)
           values ('callback', null, null, $1, $2,
                   case when exists (select 1 from job
                                     where id = $3 and status in ('done', 'failed'))
                        then now()
                        else now() + make_interval(secs => $4) end)""",
        queue.PRIORITY_BACKGROUND,
        {"target_job_id": target_job_id, "callback_url": callback_url, "key_digest": key_digest},
        target_job_id,
        float(CALLBACK_WAIT_S),
    )
    await conn.execute("select pg_notify($1, '0')", queue.CHANNEL_NEW)


async def deliver(http: httpx.AsyncClient, payload: dict[str, Any], body: dict[str, Any]) -> None:
    """Raise on failure so the queue retries; return on 2xx."""
    url = payload["callback_url"]
    try:
        await check_url(url)
    except UnsafeUrl as e:
        raise RuntimeError(f"callback url rejected: {e}") from e
    raw = json.dumps(body, separators=(",", ":"), default=str).encode()
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-TokenSage-Signature": sign(payload["key_digest"], ts, raw),
        "X-TokenSage-Timestamp": str(ts),
        "X-TokenSage-Job": str(payload["target_job_id"]),
    }
    try:
        async with asyncio.timeout(CALLBACK_TOTAL_TIMEOUT_S):
            status = await _post(http, url, raw, headers)
    except TimeoutError as e:
        raise RuntimeError(
            f"callback timed out: no complete response within {CALLBACK_TOTAL_TIMEOUT_S:.0f}s"
        ) from e
    if status >= 300:
        raise RuntimeError(f"callback returned http {status}")
    # the URL itself stays out of the logs: webhook URLs often carry a secret in the query
    log.info(
        "callback.delivered",
        host=urlsplit(url).hostname,
        job=payload["target_job_id"],
        status=status,
    )


async def _post(http: httpx.AsyncClient, url: str, raw: bytes, headers: dict[str, str]) -> int:
    """POST and return the status; the response body is read only up to
    CALLBACK_BODY_MAX_BYTES and then dropped."""
    async with http.stream(
        "POST", url, content=raw, headers=headers, timeout=8.0, follow_redirects=False
    ) as r:
        got = 0
        async for chunk in r.aiter_bytes():
            got += len(chunk)
            if got >= CALLBACK_BODY_MAX_BYTES:
                break
        return r.status_code
