"""Google Trends "trending now" RSS: what people started searching for in the last hours.

Wikipedia's daily top-1000 lags a day; this feed lists a search within minutes of it taking
off, with an approximate search count and the news stories behind it. Each feed carries only
about ten items, so the caller polls it every few minutes and keeps what it saw (fulldepth).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("gtrends")
URL = "https://trends.google.com/trending/rss?geo={geo}"
GEOS = ("US", "GB", "CA", "AU")
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_TRAFFIC = re.compile(r"<ht:approx_traffic>\s*([\d,]+)\+?\s*</ht:approx_traffic>")
_PUB = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_NEWS_TITLE = re.compile(
    r"<ht:news_item_title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</ht:news_item_title>", re.S
)


@dataclass
class TrendingSearch:
    term: str
    traffic: int  # Google's approximate search count ("2000+" -> 2000)
    started_at: datetime | None  # when Google listed it
    headline: str | None  # the first news story behind it
    geo: str


def parse(text: str, geo: str) -> list[TrendingSearch]:
    out: list[TrendingSearch] = []
    for m in _ITEM.finditer(text):
        block = m.group(1)
        t = _TITLE.search(block)
        if not t:
            continue
        term = html.unescape(t.group(1)).strip()
        if not term:
            continue
        tr = _TRAFFIC.search(block)
        p = _PUB.search(block)
        n = _NEWS_TITLE.search(block)
        out.append(
            TrendingSearch(
                term=term,
                traffic=int(tr.group(1).replace(",", "")) if tr else 0,
                started_at=parse_date(p.group(1)) if p else None,
                headline=(html.unescape(n.group(1)).strip()[:200] or None) if n else None,
                geo=geo,
            )
        )
    return out


def parse_date(raw: str | None) -> datetime | None:
    """An RSS pubDate ("Wed, 7 Oct 2026 12:10:00 -0700") as an aware UTC datetime."""
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


async def trending(http: httpx.AsyncClient, geo: str) -> list[TrendingSearch] | None:
    """The searches trending in one country now, or None when the feed is unavailable."""
    src = "gtrends"
    if not breaker.allow(src):
        return None
    try:
        r = await http.get(URL.format(geo=geo), timeout=6.0)
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("gtrends.error", geo=geo, error=f"{type(e).__name__}: {e}"[:120])
        return None
    if r.status_code != 200 or "<rss" not in r.text[:500]:
        breaker.failure(src)
        log.info("gtrends.bad_response", geo=geo, status=r.status_code)
        return None
    breaker.success(src)
    return parse(r.text, geo)
