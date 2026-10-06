"""CoinGecko (free Demo key): refresh the known-coin table from meme categories."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("coingecko")
BASE = "https://api.coingecko.com/api/v3"
CATEGORIES = ["pump-fun", "solana-meme-coins", "meme-token", "ai-meme-coins", "politifi"]
CATEGORY_LABELS = {
    "pump-fun": [],
    "solana-meme-coins": [],
    "meme-token": [],
    "ai-meme-coins": ["ai_agent"],
    "politifi": ["political"],
}


@dataclass
class GeckoCoin:
    id: str
    symbol: str
    name: str
    image: str | None
    market_cap_rank: int | None
    categories: list[str]


async def category_markets(
    http: httpx.AsyncClient, api_key: str, category: str, per_page: int = 250
) -> list[GeckoCoin] | None:
    src = "coingecko"
    if not breaker.allow(src):
        return None
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    try:
        r = await http.get(
            f"{BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "category": category,
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": 1,
            },
            headers=headers,
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("coingecko.error", error=str(e)[:120])
        return None
    if r.status_code == 404:
        breaker.success(src)
        return []  # unknown category id
    if r.status_code != 200:
        breaker.failure(src)
        return None
    breaker.success(src)
    try:
        items = r.json()
    except ValueError:
        return None
    out: list[GeckoCoin] = []
    for it in items or []:
        if not it.get("id") or not it.get("symbol"):
            continue
        out.append(
            GeckoCoin(
                id=str(it["id"]),
                symbol=str(it["symbol"]).upper()[:20],
                name=str(it.get("name") or it["id"])[:120],
                image=it.get("image"),
                market_cap_rank=it.get("market_cap_rank"),
                categories=list(CATEGORY_LABELS.get(category, [])),
            )
        )
    return out
