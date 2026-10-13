"""GeckoTerminal (free, no key): the day's most-traded pump.fun tokens, for the current-meta
signal (engine/meta.py). Pools on the pump.fun bonding curve and on PumpSwap, sorted by 24 h
volume; a token with several pools counts once, with their volumes summed."""

from __future__ import annotations

import asyncio
import time
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


# ----------------------------------------------------------------- story heat (rules 0.34.0)


@dataclass
class PoolQuote:
    """A pool's base token and how it trades now (search results)."""

    pool: str
    mint: str
    name: str
    symbol: str
    mcap_usd: float | None  # market cap, else fully diluted value
    change_5m_pct: float | None
    change_1h_pct: float | None
    volume_5m_usd: float
    volume_1h_usd: float
    created_at: str | None


class RateLimited(Exception):
    """GeckoTerminal said 429, or this process already spent its share of the minute."""


# The free tier allows about 30 calls a minute per IP; the story-heat lookups keep under
# this many in any 60 s so the daily top-volume pull still gets through.
HEAT_CALLS_PER_MIN = 20
_calls: list[float] = []


def _spend() -> None:
    now = time.monotonic()
    while _calls and now - _calls[0] >= 60:
        _calls.pop(0)
    if len(_calls) >= HEAT_CALLS_PER_MIN:
        raise RateLimited("story-heat budget spent for this minute")
    _calls.append(now)


async def _get(http: httpx.AsyncClient, path: str, params: dict) -> dict | None:
    """One budgeted GET. None on failure; RateLimited when over budget or on 429."""
    src = "geckoterminal"
    if not breaker.allow(src):
        return None
    _spend()
    try:
        r = await http.get(
            f"{BASE}{path}", params=params, headers={"Accept": "application/json"}, timeout=10.0
        )
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("geckoterminal.error", error=str(e)[:120])
        return None
    if r.status_code == 429:
        raise RateLimited("429")
    if r.status_code != 200:
        if r.status_code != 404:
            breaker.failure(src)
        log.info("geckoterminal.status", status=r.status_code, path=path[:60])
        return None
    breaker.success(src)
    try:
        doc = r.json()
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN


def parse_pools(doc: dict) -> list[PoolQuote]:
    tokens: dict[str, dict] = {}
    included = doc.get("included")
    for i in included if isinstance(included, list) else []:
        if isinstance(i, dict) and i.get("type") == "token":
            attrs = i.get("attributes")
            tokens[str(i.get("id"))] = attrs if isinstance(attrs, dict) else {}
    out: list[PoolQuote] = []
    data = doc.get("data")
    for pool in data if isinstance(data, list) else []:
        try:
            a = pool["attributes"]
            base_id = str(pool["relationships"]["base_token"]["data"]["id"])
        except (KeyError, TypeError):
            continue
        if not isinstance(a, dict):
            continue
        t = tokens.get(base_id) or {}
        mint = str(t.get("address") or base_id.removeprefix("solana_"))
        if not mint or mint in _SKIP_MINTS:
            continue
        raw_chg, raw_vol = a.get("price_change_percentage"), a.get("volume_usd")
        chg: dict = raw_chg if isinstance(raw_chg, dict) else {}
        vol: dict = raw_vol if isinstance(raw_vol, dict) else {}
        created = a.get("pool_created_at")
        out.append(
            PoolQuote(
                pool=str(a.get("address") or ""),
                mint=mint,
                name=str(t.get("name") or a.get("name") or "")[:120],
                symbol=str(t.get("symbol") or "").upper()[:20],
                mcap_usd=_num(a.get("market_cap_usd")) or _num(a.get("fdv_usd")),
                change_5m_pct=_num(chg.get("m5")),
                change_1h_pct=_num(chg.get("h1")),
                volume_5m_usd=_num(vol.get("m5")) or 0.0,
                volume_1h_usd=_num(vol.get("h1")) or 0.0,
                created_at=created if isinstance(created, str) else None,
            )
        )
    return out


async def search_pools(http: httpx.AsyncClient, query: str) -> list[PoolQuote] | None:
    """Solana pools whose token matches the query, as GeckoTerminal ranks them (one page,
    20 pools). None on failure; raises RateLimited."""
    doc = await _get(
        http,
        "/search/pools",
        {"query": query[:80], "network": "solana", "include": "base_token", "page": 1},
    )
    return None if doc is None else parse_pools(doc)


@dataclass
class Peak:
    minutes_ago: int  # since the highest 15-minute high of the last 24 h
    from_peak_pct: float  # the latest close against that high, <= 0


def parse_peak(doc: dict, now_s: float) -> Peak | None:
    try:
        rows = doc["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return None
    best: tuple[float, float] | None = None  # (high, ts)
    last: tuple[float, float] | None = None  # (ts, close)
    for r in rows if isinstance(rows, list) else []:
        try:
            ts, high, close = float(r[0]), float(r[2]), float(r[4])
        except (TypeError, ValueError, IndexError):
            continue
        if best is None or high > best[0]:
            best = (high, ts)
        if last is None or ts > last[0]:
            last = (ts, close)
    if best is None or last is None or best[0] <= 0:
        return None
    return Peak(
        minutes_ago=max(int((now_s - best[1]) / 60), 0),
        from_peak_pct=round((last[1] / best[0] - 1) * 100, 1),
    )


async def pool_peak(http: httpx.AsyncClient, pool: str) -> Peak | None:
    """When the pool's price peaked in the last 24 h (15-minute candles). None on failure;
    raises RateLimited."""
    doc = await _get(
        http,
        f"/networks/solana/pools/{pool}/ohlcv/minute",
        {"aggregate": 15, "limit": 96, "currency": "usd", "token": "base"},
    )
    return None if doc is None else parse_peak(doc, time.time())
