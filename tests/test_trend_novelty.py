"""Trend novelty (rules 0.33.0): how often a term trended before, its direction, and the
0-1 novelty built from them."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: F401 - used by the fixture's type
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import respx

from tests.conftest import needs_db
from tests.test_trend_sources import _t24, db  # noqa: F401 - the fixture
from tokensage import fulldepth
from tokensage.engine import trends
from tokensage.sources import gtrends, xtrends

NOW = datetime(2026, 10, 13, 20, 0, tzinfo=UTC)


def _t(days: int | None, hours: float, direction: str | None = None) -> trends.TrendTerm:
    return trends.TrendTerm(
        "Moo Deng",
        0.0,
        3,
        "x_trends",
        seen_at=NOW - timedelta(hours=hours),
        rank=3,
        days_listed=days,
        direction=direction,
    )


def test_novelty_is_rare_times_fresh_times_direction() -> None:
    assert trends.novelty(_t(0, 0, "rising"), NOW) == 1.0
    # listed every day for a month, on the list all day: "Elon"
    assert trends.novelty(_t(30, 24, "steady"), NOW) == round(1 / 16 * 0.5 * 0.8, 3)
    # a day old halves it, falling halves it again
    assert trends.novelty(_t(0, 24, "rising"), NOW) == 0.5
    assert trends.novelty(_t(0, 0, "falling"), NOW) == 0.5
    # news and Bluesky keep no list: no novelty
    assert trends.novelty(_t(None, 0), NOW) is None


def test_x_direction_from_the_hourly_lists() -> None:
    h3, h2, h1 = (NOW - timedelta(hours=n) for n in (3, 2, 1))
    pages = [
        [
            xtrends.TrendList(h3, ["Faller", "Gone", "Steady", "Climber"]),
            xtrends.TrendList(h2, ["Faller", "Climber", "Steady"]),
            xtrends.TrendList(h1, ["Climber"] + [f"Topic {i}" for i in range(12)] + ["Faller"]),
        ],
        [xtrends.TrendList(h1, ["Other", "Steady"])],
    ]
    got = {d["term"]: d["direction"] for d in fulldepth._merge_x_lists(pages, NOW)}
    # up from 4th to 1st; off the newest list; slipped 12 places below its best
    assert got["Climber"] == "rising"
    assert got["Gone"] == "falling"
    assert got["Faller"] == "falling"
    assert got["Steady"] == "rising"  # 3rd first, 2nd on the newest list (another region)
    assert got["Other"] == "steady"


def test_search_direction_from_the_polls() -> None:
    seen: dict[str, dict] = {}
    first = gtrends.TrendingSearch("hurricane rachel", 2000, NOW - timedelta(hours=2), None, "US")
    fulldepth._merge_search(seen, first, NOW - timedelta(minutes=20))
    v = seen["hurricane rachel"]
    assert fulldepth._search_direction(v, NOW - timedelta(minutes=20)) == "steady"
    # more searches on a later poll
    more = gtrends.TrendingSearch("hurricane rachel", 20000, NOW - timedelta(hours=2), None, "US")
    fulldepth._merge_search(seen, more, NOW)
    assert fulldepth._search_direction(v, NOW) == "rising"
    # absent from the next poll
    assert fulldepth._search_direction(v, NOW + timedelta(minutes=10)) == "falling"
    # stored before rules 0.33.0: no direction
    assert fulldepth._search_direction({"term": "x", "traffic": 5}, NOW) is None


@needs_db
async def test_history_counts_earlier_days_and_records_polls(db: asyncpg.Connection) -> None:  # noqa: F811
    today = date.today()
    # "Elon" on X's list on 20 earlier days; "Moo Deng" only yesterday (the current run)
    rows = [("elon", "x_trends", -1.0, today - timedelta(days=d)) for d in range(2, 22)]
    rows.append(("moo deng", "x_trends", -1.0, today - timedelta(days=1)))
    await db.executemany(
        """insert into trend_term (term, source, score, first_seen, day, views)
           values ($1, $2, $3, $4, $4, 1)""",
        rows,
    )
    now = datetime.now(UTC)
    page = _t24([(now - timedelta(minutes=30), ["#MooDeng", "Elon"])])
    with respx.mock(assert_all_called=False) as router:
        router.get("https://trends24.in/united-states/").mock(
            return_value=httpx.Response(200, text=page)
        )
        router.get(url__startswith="https://trends24.in/").mock(return_value=httpx.Response(503))
        router.get(url__startswith="https://trends.google.com/").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            idx = await fulldepth.trend_index(db, http)
    by = {t.term: t for t in idx.terms if t.source == "x_trends"}
    assert by["Elon"].days_listed == 20 and by["Moo Deng"].days_listed == 0
    novelty = {k: trends.novelty(t) for k, t in by.items()}
    assert novelty["Moo Deng"] is not None and novelty["Elon"] is not None
    assert novelty["Moo Deng"] > 0.5 > 0.1 > novelty["Elon"]
    x = next(s for s in idx.sources if s.source == "x_trends")
    assert x.history_days == 20
    # this poll is recorded for tomorrow's history
    got = await db.fetchval(
        "select count(*) from trend_term where source='x_trends' and day=current_date"
    )
    assert got == 2
