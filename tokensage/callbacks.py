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

import hashlib
import hmac
import json
import time
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage import queue
from tokensage.net.safe_fetch import UnsafeUrl, check_url

log = structlog.get_logger("callbacks")
CALLBACK_RETRY_S = 30
CALLBACK_MAX_ATTEMPTS = 3


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
        """insert into job (kind, mint, depth, priority, payload)
           values ('callback', null, null, $1, $2)""",
        queue.PRIORITY_BACKGROUND,
        {"target_job_id": target_job_id, "callback_url": callback_url, "key_digest": key_digest},
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
    r = await http.post(url, content=raw, headers=headers, timeout=8.0, follow_redirects=False)
    if r.status_code >= 300:
        raise RuntimeError(f"callback returned http {r.status_code}")
    log.info("callback.delivered", url=url, job=payload["target_job_id"], status=r.status_code)
