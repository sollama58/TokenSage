"""Best-effort external lookups for copycat context: other coins with the same name/ticker.

DexScreener search (free), throttled by the per-source circuit breaker; it never raises.
pump.fun's frontend-api search was asked too until its route went away (404 since
2026-10-17); it is no longer called.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
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
    return await _dexscreener_fetch(http, term) or []


async def _dexscreener_fetch(http: httpx.AsyncClient, term: str) -> list[SameNameToken] | None:
    """The search's Solana results; None when it was not asked or did not answer (so a
    failure is never cached as "no namesakes")."""
    src = "dexscreener_search"
    if not term or not breaker.allow(src):
        return None
    try:
        r = await http.get(DEX_SEARCH, params={"q": term}, timeout=5.0)
        if r.status_code != 200:
            if r.status_code != 404:
                breaker.failure(src)
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        breaker.failure(src)
        log.info("lookups.dexscreener_failed", error=str(e)[:120])
        return None
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


# The external name search, per search term, kept DEX_SEARCH_TTL_S (user ask 2026-10-23: API
# audit): a copycat wave analyses dozens of coins under one name within minutes, and each
# used to ask DexScreener the same question. The cost is that a namesake launched within the
# TTL is missing from the *external* list; TokenSage's own database still finds every
# namesake it has analysed. Only answers are kept: a failure is asked again next time.
DEX_SEARCH_TTL_S = 600.0
DEX_SEARCH_MAX_TERMS = 1024


def _search_key(term: str) -> str:
    return " ".join(term.split()).casefold()


class _SearchMemo:
    """Per-process single flight and bounded, short-lived memo of DexScreener searches, keyed
    by the normalised term: the worker slots analysing a copycat wave ask at once, and only
    the first should fetch."""

    def __init__(self) -> None:
        self.done: OrderedDict[str, tuple[float, list[SameNameToken]]] = OrderedDict()
        self.inflight: dict[str, asyncio.Task[list[SameNameToken] | None]] = {}

    def clear(self) -> None:
        self.done.clear()
        self.inflight.clear()

    async def get(self, http: httpx.AsyncClient, term: str) -> list[SameNameToken]:
        key = _search_key(term)
        if not key:
            return []
        hit = self.done.get(key)
        if hit is not None:
            if hit[0] > time.monotonic():
                self.done.move_to_end(key)
                return list(hit[1])
            del self.done[key]
        loop = asyncio.get_running_loop()
        task = self.inflight.get(key)
        if task is None or task.get_loop() is not loop:
            task = loop.create_task(self._run(key, http, term))
            # retrieved here, so a failure nobody is left waiting for logs no warning
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self.inflight[key] = task
        # shielded: a reader that gives up (its deadline) does not cancel the others' search
        got = await asyncio.shield(task)
        return list(got) if got is not None else []

    async def _run(
        self, key: str, http: httpx.AsyncClient, term: str
    ) -> list[SameNameToken] | None:
        try:
            got = await _dexscreener_fetch(http, term)
            if got is not None:
                self.done[key] = (time.monotonic() + DEX_SEARCH_TTL_S, got)
                self.done.move_to_end(key)
                while len(self.done) > DEX_SEARCH_MAX_TERMS:
                    self.done.popitem(last=False)
            return got
        finally:
            if self.inflight.get(key) is asyncio.current_task():
                del self.inflight[key]


search_memo = _SearchMemo()


def dexscreener_search_fresh(term: str) -> bool:
    """Whether the memo holds an unexpired answer for the term (no network, no side effect)."""
    hit = search_memo.done.get(_search_key(term))
    return hit is not None and hit[0] > time.monotonic()


async def dexscreener_search_cached(http: httpx.AsyncClient, term: str) -> list[SameNameToken]:
    """dexscreener_search, shared per normalised term for DEX_SEARCH_TTL_S; never raises
    (beyond cancellation)."""
    return await search_memo.get(http, term)


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
