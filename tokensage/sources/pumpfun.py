"""pump.fun's single-coin read (frontend-api-v3 coins-v2), relayed for TrenchScanner.

From 2026-10-22 pump.fun's Cloudflare answers every TrenchScanner request with its managed
challenge (a 403 "Just a moment..." page), Origin and User-Agent headers included (#1128 there),
while this service, from the same Render region, is answered (5,152 of 5,152 on 2026-10-29). The
cause is either Node's client fingerprint or TrenchScanner's call rate (check-in 2026-10-29).
This relay is the test and, if it holds, the route: TrenchScanner asks here for a coin it was
refused, and the answer says whether pump.fun answered us. Its volume stays low on purpose (a
shared per-minute cap and a cache), so that a rate-based block does not spread to this
service's own reads (resolve/resolver.py's creation time).

The coin is hostile input: only whitelisted scalar fields are passed on, each type-checked and
clipped.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("pumpfun")

URL = "https://frontend-api-v3.pump.fun/coins-v2/{mint}"
SOURCE = "pumpfun_relay"
TIMEOUT_S = 6.0
# A 403 holds the route this long before one request asks again (resolver.py's
# PUMP_COINS_BLOCKED_S): every caller in between gets the same refusal without a call.
BLOCKED_S = 60.0
# Upstream calls a minute, every key together. TrenchScanner's launch lookups ask for up to 60
# coins a cycle; this keeps the relay near this service's own rate (~4 a minute on 10-29).
MAX_PER_MIN = 30
# An answer (a coin, or none) is served again for this long; launch details don't change.
CACHE_S = 600.0
CACHE_MAX = 2000

STR_FIELDS: dict[str, int] = {
    "name": 200,
    "symbol": 50,
    "description": 2000,
    "image_uri": 500,
    "twitter": 300,
    "telegram": 300,
    "website": 300,
    "creator": 64,
    "mayhem_state": 20,
    "charity_name": 200,
}
NUM_FIELDS = ("created_timestamp", "usd_market_cap", "market_cap_usd")
BOOL_FIELDS = ("complete", "is_currently_live", "is_charity")

Status = Literal["ok", "not_found", "refused", "unavailable", "busy"]


@dataclass
class Answer:
    status: Status
    coin: dict[str, Any] | None = None
    upstream_status: int | None = None
    # Cloudflare's cf-mitigated header on a refusal ("challenge" for the managed challenge)
    cf_mitigated: str | None = None
    cached: bool = False


@dataclass
class _Relay:
    calls: deque[float] = field(default_factory=deque)
    cache: OrderedDict[str, tuple[float, Answer]] = field(default_factory=OrderedDict)
    last_refusal: Answer | None = None

    def clear(self) -> None:
        self.calls.clear()
        self.cache.clear()
        self.last_refusal = None


relay = _Relay()


def clean_coin(raw: Any, mint: str) -> dict[str, Any] | None:
    """The whitelisted fields of a coins-v2 payload, or None when it is not this coin."""
    if not isinstance(raw, dict) or raw.get("mint") != mint:
        return None
    out: dict[str, Any] = {"mint": mint}
    for k, cap in STR_FIELDS.items():
        v = raw.get(k)
        if isinstance(v, str) and v:
            out[k] = v[:cap]
    for k in NUM_FIELDS:
        v = raw.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool) and v == v and abs(v) < 1e18:
            out[k] = v
    for k in BOOL_FIELDS:
        v = raw.get(k)
        if isinstance(v, bool):
            out[k] = v
    return out


def _cached(mint: str, now: float) -> Answer | None:
    hit = relay.cache.get(mint)
    if hit is None:
        return None
    at, answer = hit
    if now - at > CACHE_S:
        del relay.cache[mint]
        return None
    return Answer(answer.status, answer.coin, answer.upstream_status, None, cached=True)


def _remember(mint: str, answer: Answer, now: float) -> None:
    relay.cache[mint] = (now, answer)
    relay.cache.move_to_end(mint)
    while len(relay.cache) > CACHE_MAX:
        relay.cache.popitem(last=False)


def _take_slot(now: float) -> bool:
    while relay.calls and now - relay.calls[0] >= 60.0:
        relay.calls.popleft()
    if len(relay.calls) >= MAX_PER_MIN:
        return False
    relay.calls.append(now)
    return True


async def coin(http: httpx.AsyncClient, mint: str) -> Answer:
    """pump.fun's coins-v2 for `mint` (already validated by parse_ca), through the cap."""
    now = time.monotonic()
    hit = _cached(mint, now)
    if hit is not None:
        return hit
    if not breaker.allow_probe(SOURCE, TIMEOUT_S):
        last = relay.last_refusal
        if last is not None:
            return Answer("refused", None, last.upstream_status, last.cf_mitigated)
        return Answer("unavailable")
    if not _take_slot(now):
        return Answer("busy")
    try:
        r = await http.get(
            URL.format(mint=mint),
            headers={"Origin": "https://pump.fun", "Accept": "application/json"},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        breaker.failure(SOURCE)
        log.info("pumpfun.relay_failed", error=type(e).__name__)
        return Answer("unavailable")
    if r.status_code == 403:
        breaker.open_for(SOURCE, BLOCKED_S)
        refused = Answer("refused", None, 403, (r.headers.get("cf-mitigated") or "")[:40] or None)
        relay.last_refusal = refused
        log.warning("pumpfun.relay_refused", cf_mitigated=refused.cf_mitigated)
        return refused
    if r.status_code == 429 or r.status_code >= 500:
        breaker.failure(SOURCE)
        return Answer("unavailable", None, r.status_code)
    breaker.success(SOURCE)
    relay.last_refusal = None
    if r.status_code == 404:
        answer = Answer("not_found", None, 404)
    elif r.status_code != 200:
        return Answer("unavailable", None, r.status_code)
    else:
        try:
            got = clean_coin(r.json(), mint)
        except ValueError:
            return Answer("unavailable", None, 200)
        answer = Answer("ok", got, 200) if got else Answer("not_found", None, 200)
    _remember(mint, answer, now)
    return answer
