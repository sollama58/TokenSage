"""X trending topics, from trends24.in: X's own top-50 trend list per country, one list an hour
for the last 24 hours. The X API has no free trends endpoint; trends24 republishes the list
as a public page. Each page carries the whole day, so one poll every few minutes (fulldepth)
is enough. Free: a handful of page loads an hour, nothing per coin.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("xtrends")
URL = "https://trends24.in/{region}"
# "" is the worldwide list
REGIONS = ("", "united-states/", "united-kingdom/", "canada/", "australia/")
_CARD = re.compile(
    r'data-timestamp=([\d.]+)[^>]*>.*?<ol class=["\']?trend-card__list["\']?>(.*?)</ol>', re.S
)
_LINK = re.compile(r"class=[\"']?trend-link[\"']?>([^<]+)</a>")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass
class TrendList:
    at: datetime  # when trends24 took this hourly snapshot
    terms: list[str]  # in rank order (1 first)


def parse(text: str) -> list[TrendList]:
    out: list[TrendList] = []
    for m in _CARD.finditer(text):
        try:
            at = datetime.fromtimestamp(float(m.group(1)), tz=UTC)
        except (ValueError, OverflowError, OSError):
            continue
        terms = [html.unescape(t).strip() for t in _LINK.findall(m.group(2))]
        terms = [t for t in terms if t]
        if terms:
            out.append(TrendList(at, terms))
    return out


def readable(term: str) -> str | None:
    """A trend as words to match coin names against: "#MooDeng" -> "Moo Deng". Cashtags
    ($XYZ) are coins being shilled, not subjects trending: None."""
    t = term.strip()
    if not t or t.startswith("$"):
        return None
    if t.startswith("#"):
        t = _CAMEL.sub(" ", t[1:]).replace("_", " ")
    t = " ".join(t.split())
    return t or None


async def trending(http: httpx.AsyncClient, region: str) -> list[TrendList] | None:
    """The hourly trend lists of the last day for one region, or None when unavailable."""
    src = "xtrends"
    if not breaker.allow(src):
        return None
    try:
        r = await http.get(URL.format(region=region), timeout=8.0)
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("xtrends.error", region=region, error=f"{type(e).__name__}: {e}"[:120])
        return None
    lists = parse(r.text) if r.status_code == 200 else []
    if not lists:
        breaker.failure(src)
        log.info("xtrends.bad_response", region=region, status=r.status_code)
        return None
    breaker.success(src)
    return lists
