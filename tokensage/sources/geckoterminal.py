"""GeckoTerminal (free, no key): the day's most-traded pump.fun tokens, for the current-meta
signal (engine/meta.py). Pools on the pump.fun bonding curve and on PumpSwap, sorted by 24 h
volume; a token with several pools counts once, with their volumes summed."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("geckoterminal")
BASE = "https://api.geckoterminal.com/api/v2"
# Quote and base-side majors that are never a meme's subject (a SOL/USDC pool on a pump dex)
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


@dataclass
class TopToken:
    mint: str
    name: str
    symbol: str
    volume_usd: float  # 24 h, summed over its pools
    dex: str  # the dex of its busiest pool


async def top_pools(http: httpx.AsyncClient, dex: str, page: int = 1) -> list[TopToken] | None:
    """One page (20 pools) of a dex's pools by 24 h volume, as base tokens. None on failure."""
    src = "geckoterminal"
    if not breaker.allow(src):
        return None
    try:
        r = await http.get(
            f"{BASE}/networks/solana/dexes/{dex}/pools",
            params={"sort": "h24_volume_usd_desc", "page": page, "include": "base_token"},
            headers={"Accept": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("geckoterminal.error", error=str(e)[:120])
        return None
    if r.status_code != 200:
        if r.status_code != 429:  # rate limited: back off, but the source is fine
            breaker.failure(src)
        log.info("geckoterminal.status", status=r.status_code, dex=dex)
        return None
    breaker.success(src)
    try:
        doc = r.json()
    except ValueError:
        return None
    if not isinstance(doc, dict):
        log.info("geckoterminal.bad_body", dex=dex, type=type(doc).__name__)
        return None
    included = doc.get("included")
    tokens: dict[str, dict] = {}
    for i in included if isinstance(included, list) else []:
        if isinstance(i, dict) and i.get("type") == "token":
            attrs = i.get("attributes")
            tokens[str(i.get("id"))] = attrs if isinstance(attrs, dict) else {}
    out: list[TopToken] = []
    pools = doc.get("data")
    for pool in pools if isinstance(pools, list) else []:
        try:
            base_id = pool["relationships"]["base_token"]["data"]["id"]
            vol = float(pool["attributes"]["volume_usd"]["h24"] or 0)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        t = tokens.get(str(base_id)) or {}
        mint = str(t.get("address") or str(base_id).removeprefix("solana_"))
        if not mint or mint in _SKIP_MINTS:
            continue
        out.append(
            TopToken(
                mint=mint,
                name=str(t.get("name") or "")[:120],
                symbol=str(t.get("symbol") or "").upper()[:20],
                volume_usd=vol,
                dex=dex,
            )
        )
    return out


def merge_top(pages: list[list[TopToken]], count: int) -> list[TopToken]:
    """One row per token (volumes of its pools summed), the `count` busiest first."""
    by_mint: dict[str, TopToken] = {}
    best: dict[str, float] = {}
    for page in pages:
        for t in page:
            cur = by_mint.get(t.mint)
            if cur is None:
                by_mint[t.mint] = TopToken(t.mint, t.name, t.symbol, t.volume_usd, t.dex)
                best[t.mint] = t.volume_usd
                continue
            cur.volume_usd += t.volume_usd
            if t.volume_usd > best[t.mint]:
                best[t.mint], cur.dex = t.volume_usd, t.dex
            cur.name = cur.name or t.name
            cur.symbol = cur.symbol or t.symbol
    return sorted(by_mint.values(), key=lambda t: -t.volume_usd)[:count]


async def top_tokens(
    http: httpx.AsyncClient, dexes: list[str], pages: int, count: int, pause_s: float = 6.0
) -> list[TopToken] | None:
    """The `count` most-traded tokens over the given dexes. None when no page came back
    (so the caller keeps yesterday's list instead of storing an empty day)."""
    got: list[list[TopToken]] = []
    for dex in dexes:
        for page in range(1, pages + 1):
            res = await top_pools(http, dex, page)
            if res is not None:
                got.append(res)
            await asyncio.sleep(pause_s)  # free tier: a few requests per minute
    if not got:
        return None
    return merge_top(got, count)
