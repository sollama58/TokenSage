"""Wikipedia search for names nothing in the gazetteer knows (full depth only).

One `action=query&generator=search` call returns the top articles with their short
description and Wikidata id. Throttled by the circuit breaker; never raises. Wikimedia
asks for a descriptive User-Agent (the shared client sends one) and allows about 200
requests a minute with it; results are cached by the caller (fulldepth.wiki_search).

Wikimedia's rate-limit rules (mediawiki.org/wiki/Wikimedia_APIs/Rate_limits) also ask for
at most 3 concurrent requests and for Retry-After to be respected on 429/503. The worker
runs several analyses at once, so both are enforced here: a semaphore caps requests in
flight, and a 429/503 pauses every lookup until Retry-After (5 s when absent) has passed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("wikipedia")

API = "https://en.wikipedia.org/w/api.php"
MAX_CONCURRENT = 3
DEFAULT_RETRY_AFTER_S = 5.0
MAX_RETRY_AFTER_S = 300.0

_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_paused_until = 0.0  # time.monotonic() before which Wikipedia is not asked


def _retry_after(r: httpx.Response) -> float:
    try:
        s = float(r.headers.get("retry-after", ""))
    except ValueError:
        s = DEFAULT_RETRY_AFTER_S  # missing, or an HTTP date: wait the policy's minimum
    return min(max(s, DEFAULT_RETRY_AFTER_S), MAX_RETRY_AFTER_S)


def _semaphore() -> asyncio.Semaphore:
    """The cap on requests in flight, one per event loop (a semaphore is bound to its loop)."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(MAX_CONCURRENT))
    return _slots[1]


def reset() -> None:
    """Forget a pause (tests)."""
    global _paused_until
    _paused_until = 0.0


@dataclass(frozen=True)
class WikiPage:
    title: str
    desc: str
    qid: str | None
    rank: int  # 1 = the search's best result
    disambiguation: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> WikiPage:
        return cls(
            title=str(d.get("title") or ""),
            desc=str(d.get("desc") or ""),
            qid=d.get("qid"),
            rank=int(d.get("rank") or 99),
            disambiguation=bool(d.get("disambiguation")),
        )


def parse(data: object) -> list[WikiPage]:
    query = data.get("query") if isinstance(data, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list):
        return []
    out: list[WikiPage] = []
    for p in pages:
        if not isinstance(p, dict) or not p.get("title") or p.get("missing"):
            continue
        props = p.get("pageprops") or {}
        out.append(
            WikiPage(
                title=str(p["title"]),
                desc=str(p.get("description") or ""),
                qid=props.get("wikibase_item"),
                rank=int(p.get("index") or 99),
                disambiguation="disambiguation" in props,
            )
        )
    return sorted(out, key=lambda x: x.rank)


async def search(http: httpx.AsyncClient, query: str, limit: int = 5) -> list[WikiPage] | None:
    """The top articles for a query, best first; None when Wikipedia could not be asked."""
    global _paused_until
    src = "wikipedia_search"
    if not query or not breaker.allow(src) or time.monotonic() < _paused_until:
        return None
    try:
        async with _semaphore():
            if time.monotonic() < _paused_until:
                return None  # another lookup was told to back off while this one waited
            r = await http.get(
                API,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrlimit": limit,
                    "gsrnamespace": 0,
                    "prop": "description|pageprops",
                    "ppprop": "wikibase_item|disambiguation",
                    "redirects": 1,
                    "format": "json",
                    "formatversion": 2,
                },
                timeout=5.0,
            )
        if r.status_code in (429, 503):
            wait = _retry_after(r)
            _paused_until = max(_paused_until, time.monotonic() + wait)
            log.info("wikipedia.backoff", status=r.status_code, wait_s=wait)
            return None
        if r.status_code != 200:
            breaker.failure(src)
            log.info("wikipedia.status", status=r.status_code)
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        breaker.failure(src)
        log.info("wikipedia.error", error=str(e)[:120])
        return None
    breaker.success(src)
    return parse(data)
