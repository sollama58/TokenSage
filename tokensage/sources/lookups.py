"""Best-effort external lookups for copycat context: other coins with the same name/ticker.

DexScreener search (free), throttled by the per-source circuit breaker; it never raises.
pump.fun's frontend-api search was asked too until its route went away (404 since
2026-10-17); it is no longer called.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from tokensage.engine.pipeline import SameNameToken
from tokensage.net.breaker import breaker

log = structlog.get_logger("lookups")

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search"


def _ts_ms(v: object) -> datetime | None:
    if isinstance(v, int | float) and v > 0:
        try:
            return datetime.fromtimestamp(v / 1000, tz=UTC)
        except (OverflowError, ValueError, OSError):
            return None
    return None


def _str_or_none(v: object) -> str | None:
    """A name / symbol field: strings only (a number or list there is not a name)."""
    return v if isinstance(v, str) and v else None


async def dexscreener_search(http: httpx.AsyncClient, term: str) -> list[SameNameToken]:
    src = "dexscreener_search"
    if not term or not breaker.allow(src):
        return []
    try:
        r = await http.get(DEX_SEARCH, params={"q": term}, timeout=5.0)
        if r.status_code != 200:
            if r.status_code != 404:
                breaker.failure(src)
            return []
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        breaker.failure(src)
        log.info("lookups.dexscreener_failed", error=str(e)[:120])
        return []
    breaker.success(src)
    out: list[SameNameToken] = []
    seen: set[str] = set()
    pairs = data.get("pairs") if isinstance(data, dict) else None
    for pair in pairs if isinstance(pairs, list) else []:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken")
        if not isinstance(base, dict):
            continue
        addr = base.get("address")
        if not addr or not isinstance(addr, str) or addr in seen:
            continue
        seen.add(addr)
        out.append(
            SameNameToken(
                mint=str(addr),
                name=_str_or_none(base.get("name")),
                symbol=_str_or_none(base.get("symbol")),
                created_at=_ts_ms(pair.get("pairCreatedAt")),
                source="dexscreener",
            )
        )
    return out


def filter_same_name(
    results: list[SameNameToken], ticker: str | None, name_compact: str
) -> list[SameNameToken]:
    """Keep only results whose ticker or compacted name actually matches."""
    t = (ticker or "").upper()
    out: list[SameNameToken] = []
    for r in results:
        rs = (r.symbol or "").upper()
        rn = "".join(ch for ch in (r.name or "").lower() if ch.isalnum())
        if (t and rs == t) or (name_compact and rn == name_compact):
            out.append(r)
    return out
