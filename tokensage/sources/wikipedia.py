"""Wikipedia search for names nothing in the gazetteer knows (full depth only).

One `action=query&generator=search` call returns the top articles with their short
description and Wikidata id. Throttled by the circuit breaker; never raises. Wikimedia
asks for a descriptive User-Agent (the shared client sends one) and allows about 200
requests a minute with it; results are cached by the caller (fulldepth.wiki_search).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("wikipedia")

API = "https://en.wikipedia.org/w/api.php"


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
    src = "wikipedia_search"
    if not query or not breaker.allow(src):
        return None
    try:
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
