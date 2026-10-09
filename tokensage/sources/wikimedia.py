"""Wikimedia pageviews: the daily top-1000 articles, with a spike ratio against prior days."""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("wikimedia")
TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m:02d}/{d:02d}"
SKIP = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "File:", "Help:", "Talk:")


async def top_articles(http: httpx.AsyncClient, day: date) -> dict[str, int] | None:
    """{article_title: views} for one day, or None if unavailable."""
    src = "wikimedia"
    if not breaker.allow(src):
        return None
    try:
        r = await http.get(TOP.format(y=day.year, m=day.month, d=day.day), timeout=15.0)
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("wikimedia.error", error=str(e)[:120])
        return None
    if r.status_code == 404:
        breaker.success(src)
        return None  # not published yet (about 1 day lag)
    if r.status_code != 200:
        breaker.failure(src)
        return None
    breaker.success(src)
    try:
        items = r.json()["items"][0]["articles"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    out: dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        a = it.get("article") or ""
        if not isinstance(a, str) or not a or a.startswith(SKIP):
            continue
        try:
            views = int(it.get("views") or 0)
        except (TypeError, ValueError):
            views = 0
        out[a.replace("_", " ")] = views
    return out


def spike_ratios(
    today: dict[str, int], history: dict[date, dict[str, int]], floor_quantile: float = 0.1
) -> dict[str, float]:
    """views(today) / median(views on prior days). An article missing from a prior day's
    top-1000 is assumed to have had roughly that day's lowest listed views (a floor)."""
    if not history:
        return {t: 1.0 for t in today}
    floors: dict[date, int] = {}
    for d, m in history.items():
        vals = sorted(m.values())
        floors[d] = vals[max(0, int(len(vals) * floor_quantile) - 1)] if vals else 1
    out: dict[str, float] = {}
    for title, v in today.items():
        prior = [m.get(title, floors[d]) for d, m in history.items()]
        med = statistics.median(prior) if prior else 1
        out[title] = round(v / max(med, 1), 2)
    return out


def days_back(n: int, end: date | None = None) -> list[date]:
    end = end or (date.today() - timedelta(days=1))
    return [end - timedelta(days=i) for i in range(n)]
