"""Google News RSS search: a cheap 'is this in the news right now' check (full depth only)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("gnews")
URL = "https://news.google.com/rss/search?q={q}+when:{when}&hl=en-US&gl=US&ceid=US:en"
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_SOURCE = re.compile(r"<source[^>]*>(.*?)</source>", re.S)
_PUB = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


@dataclass
class Headline:
    title: str
    source: str | None
    published: str | None


async def search(http: httpx.AsyncClient, query: str, when: str = "2d") -> list[Headline] | None:
    src = "gnews"
    if not query or not breaker.allow(src):
        return None
    try:
        r = await http.get(URL.format(q=quote_plus(query), when=when), timeout=8.0)
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("gnews.error", error=str(e)[:120])
        return None
    if r.status_code != 200 or "<rss" not in r.text[:500]:
        breaker.failure(src)
        return None
    breaker.success(src)
    out: list[Headline] = []
    for m in _ITEM.finditer(r.text):
        block = m.group(1)
        t = _TITLE.search(block)
        if not t:
            continue
        title = html.unescape(t.group(1)).strip()
        s = _SOURCE.search(block)
        p = _PUB.search(block)
        out.append(
            Headline(
                title, html.unescape(s.group(1)).strip() if s else None, p.group(1) if p else None
            )
        )
        if len(out) >= 20:
            break
    return out
