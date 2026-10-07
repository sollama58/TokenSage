"""Trend sources: Google Trends polling, per-source status, referent/alias matching, score."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tokensage.engine import trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.pipeline import EngineInput, run_full
from tokensage.sources import bluesky, gtrends, xtrends

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<title>Daily Search Trends</title>
<item>
  <title>hurricane rachel</title>
  <ht:approx_traffic>20,000+</ht:approx_traffic>
  <pubDate>Wed, 7 Oct 2026 12:10:00 -0700</pubDate>
  <ht:news_item><ht:news_item_title>Hurricane Rachel makes landfall &amp; more</ht:news_item_title>
  <ht:news_item_source>AP</ht:news_item_source></ht:news_item>
</item>
<item>
  <title>tank dell</title>
  <ht:approx_traffic>500+</ht:approx_traffic>
  <pubDate>Wed, 7 Oct 2026 11:00:00 -0700</pubDate>
</item>
</channel></rss>"""


def test_gtrends_parse() -> None:
    items = gtrends.parse(FEED, "US")
    assert [i.term for i in items] == ["hurricane rachel", "tank dell"]
    assert items[0].traffic == 20_000 and items[1].traffic == 500
    assert items[0].started_at == datetime(2026, 10, 7, 19, 10, tzinfo=UTC)
    assert items[0].headline == "Hurricane Rachel makes landfall & more"
    assert items[1].headline is None


def test_score_scales() -> None:
    k = load_knowledge()
    idx = trends.TrendIndex([], k)

    def wiki(spike: float) -> trends.TrendHit:
        return trends.TrendHit(trends.TrendTerm("Quokka", spike, 1), "quokka", "name")

    def gt(n: int) -> trends.TrendHit:
        return trends.TrendHit(trends.TrendTerm("x", 0.0, n, "google_trends"), "x", "name")

    assert trends.score(wiki(30.0), idx) == 1.0
    assert 0.3 < trends.score(wiki(3.0), idx) < trends.score(wiki(10.0), idx) < 1.0
    assert trends.score(gt(100_000)) == 1.0 and trends.score(gt(100)) == 0.05
    assert trends.score(gt(500)) < trends.score(gt(5_000)) < trends.score(gt(50_000))
    news = trends.TrendHit(trends.TrendTerm("Le Chonk", 0.0, 4, "news"), "le chonk", "name")
    assert trends.score(news) == 0.5
    # a perennial article (one of the best-known entities) counts half
    trump = trends.TrendHit(trends.TrendTerm("Donald Trump", 30.0, 1), "donald trump", "name")
    assert trends.score(trump, idx) == 0.5


def test_referent_alias_matches_trend_the_name_does_not_spell() -> None:
    """A coin named "Elons" never spells "Elon Musk", but it resolves to him: when his
    article spikes, the trend matches through the referent."""
    k = load_knowledge()
    idx = trends.TrendIndex([trends.TrendTerm("Elon Musk", 6.0, 400_000)], k)
    assert idx.match("elons", "name") == []
    out = run_full(EngineInput("m", "Elons", "ELONS", None, None, None, trend_index=idx))
    assert out.agg.referent and out.agg.referent.label == "Elon Musk"
    hits = [h for h in out.trend_hits if h.term.term == "Elon Musk"]
    assert hits and hits[0].matched_on == "referent" and hits[0].score
    ev = [e for e in out.evidence if e.kind == "trend"]
    assert ev and "its referent" in ev[0].detail
    # matched through the referent: no second, competing referent from the trend
    assert not any(e.source.startswith("wikipedia:") and e.referent for e in out.evidence)


def test_referent_match_is_exact_not_a_substring() -> None:
    k = load_knowledge()
    idx = trends.TrendIndex([trends.TrendTerm("Musk", 9.0, 300_000)], k)
    assert idx.match_exact("Elon Musk", "name", "referent") is None
    hit = idx.match_exact("musk", "name", "alias")
    assert hit is not None and hit.matched_on == "alias"


def test_google_trends_search_raises_news_event_and_referent() -> None:
    k = load_knowledge()
    seen = datetime(2026, 10, 7, 19, 10, tzinfo=UTC)
    idx = trends.TrendIndex(
        [
            trends.TrendTerm(
                "hurricane rachel",
                0.0,
                20_000,
                source="google_trends",
                seen_at=seen,
                headline="Hurricane Rachel makes landfall",
            )
        ],
        k,
    )
    out = run_full(
        EngineInput("m", "Hurricane Rachel", "RACHEL", None, None, None, trend_index=idx)
    )
    assert [h.term.source for h in out.trend_hits] == ["google_trends"]
    assert out.trend_hits[0].term.seen_at == seen
    assert dict(out.agg.categories).get("news_event", 0) >= 0.5
    assert out.agg.referent and out.agg.referent.label == "hurricane rachel"
    assert "trending search" in out.summary


@pytest.fixture
async def db(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    from tokensage import fulldepth
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    await conn.execute("truncate trend_term, lookup_cache")
    fulldepth._trend_cache = None
    try:
        yield conn
    finally:
        fulldepth._trend_cache = None
        await conn.close()


@needs_db
async def test_trend_index_reports_each_source(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth

    await db.execute(
        """insert into trend_term (term, source, score, spike, first_seen, day, views)
           values ('Quokka', 'wikipedia', 1, 12.0, $1, $1, 300000)""",
        date.today() - timedelta(days=1),
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://trends\.google\.com/trending/rss\?geo=US").mock(
            return_value=httpx.Response(200, text=FEED)
        )
        router.get(url__startswith="https://trends.google.com/").mock(
            return_value=httpx.Response(429)
        )
        async with httpx.AsyncClient() as http:
            idx = await fulldepth.trend_index(db, http)
    st = {s.source: s for s in idx.sources}
    assert st["wikipedia"].status == "ok" and st["wikipedia"].terms == 1
    g = st["google_trends"]
    assert g.status == "ok" and g.terms == 2 and g.detail and "3 of 4" in g.detail
    hits = idx.match("hurricane rachel", "name")
    assert hits and hits[0].term.source == "google_trends" and hits[0].term.views == 20_000
    # kept between polls: the next rebuild, with every feed down, still has the searches
    await db.execute("update lookup_cache set fetched_at = now() - interval '20 minutes'")
    fulldepth._trend_cache = None
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://trends.google.com/").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            idx = await fulldepth.trend_index(db, http)
    g = next(s for s in idx.sources if s.source == "google_trends")
    assert g.status == "stale" and g.terms == 2
    assert idx.match("tank dell", "name")


@needs_db
async def test_trend_index_flags_stale_and_missing_wikipedia(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth

    idx = await fulldepth.trend_index(db)
    st = {s.source: s.status for s in idx.sources}
    assert st == {"wikipedia": "unavailable", "google_trends": "skipped", "x_trends": "skipped"}
    await db.execute(
        """insert into trend_term (term, source, score, spike, first_seen, day, views)
           values ('Quokka', 'wikipedia', 1, 12.0, $1, $1, 300000)""",
        date.today() - timedelta(days=9),
    )
    fulldepth._trend_cache = None
    idx = await fulldepth.trend_index(db)
    w = next(s for s in idx.sources if s.source == "wikipedia")
    assert w.status == "stale" and w.detail and "9 days old" in w.detail


@needs_db
async def test_news_status_is_stale_when_google_news_is_down(db: asyncpg.Connection) -> None:
    """Google News failing with an hours-old cached search must not report the news source
    as fresh: the analyzer's status says stale, as of the cached fetch."""
    from tokensage import fulldepth

    await db.execute(
        """insert into lookup_cache (key, value, fetched_at)
           values ('gnews:q:le chonk', $1, now() - interval '3 hours')""",
        [{"title": "Le Chonk the cat - CNN", "source": "CNN", "published": None}],
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://news.google.com/").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            found = await fulldepth.news_lookup(db, http, "Le Chonk", exact=True)
    assert found is not None and found.stale
    assert found.headlines[0]["title"].startswith("Le Chonk")
    assert datetime.now(UTC) - found.as_of > timedelta(hours=2)


# ----------------------------------------------------------------- X trends (trends24)


def _t24(cards: list[tuple[datetime, list[str]]]) -> str:
    """A trends24 page: one hourly card per (time, topics in rank order)."""
    out = ['<div id=timeline-container><div class="px-2">']
    for at, topics in cards:
        lis = "".join(
            f'<li><span class=trend-name><a href="https://twitter.com/search?q=x" '
            f'class=trend-link>{t}</a><span class=tweet-count data-count=""></span></span></li>'
            for t in topics
        )
        out.append(
            f"<div class=list-container><h3 class=title data-timestamp={at.timestamp():.3f}>"
            f"{at:%c}</h3><ol class=trend-card__list>{lis}</ol></div>"
        )
    return "".join(out) + "</div></div>"


def test_xtrends_parse_and_readable() -> None:
    at = datetime(2026, 10, 7, 19, 42, tzinfo=UTC)
    lists = xtrends.parse(_t24([(at, ["Mall of America", "#MooDeng", "Tom &amp; Jerry"])]))
    assert len(lists) == 1 and lists[0].at == at
    assert lists[0].terms == ["Mall of America", "#MooDeng", "Tom & Jerry"]
    assert xtrends.readable("#MooDeng") == "Moo Deng"
    assert xtrends.readable("#funtoken") == "funtoken"
    assert xtrends.readable("#GOT7") == "GOT7"
    assert xtrends.readable("$PNUT") is None
    # everyday topics name no subject
    for t in ("Good Morning", "#HappyFriday", "#ThursdayThoughts", "Monday", "#GM"):
        assert xtrends.readable(t) is None, t
    assert xtrends.readable("Happy Birthday Jungkook") == "Happy Birthday Jungkook"


def test_xtrends_merge_keeps_best_rank_hours_and_first_seen() -> None:
    from tokensage import fulldepth

    now = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    h1, h2, old = now - timedelta(hours=2), now - timedelta(hours=1), now - timedelta(hours=30)
    us = [
        xtrends.TrendList(h1, ["Hamas", "#MooDeng"]),
        xtrends.TrendList(h2, ["#MooDeng", "Pump Fun Coin"]),
        xtrends.TrendList(old, ["Old Story"]),
    ]
    uk = [xtrends.TrendList(h2, ["Other", "Moo Deng"])]
    got = {d["term"].lower(): d for d in fulldepth._merge_x_lists([us, uk], now)}
    assert set(got) == {"hamas", "moo deng", "other"}  # crypto topic and >24 h dropped
    md = got["moo deng"]
    assert md["rank"] == 1 and md["hours"] == 2 and md["label"] == "#MooDeng"
    assert md["seen_at"] == h1.isoformat() and md["last_at"] == h2.isoformat()


def test_xtrends_score_and_evidence() -> None:
    k = load_knowledge()

    def xt(rank: int, hours: int) -> trends.TrendHit:
        return trends.TrendHit(
            trends.TrendTerm("Moo Deng", 0.0, hours, "x_trends", rank=rank), "moo deng", "name"
        )

    assert trends.score(xt(1, 12)) == 1.0
    assert trends.score(xt(50, 1)) < trends.score(xt(25, 3)) < trends.score(xt(3, 8))
    idx = trends.TrendIndex([trends.TrendTerm("Moo Deng", 0.0, 6, source="x_trends", rank=4)], k)
    out = run_full(EngineInput("m", "Moo Deng Wife", "MWIFE", None, None, None, trend_index=idx))
    assert [h.term.source for h in out.trend_hits] == ["x_trends"]
    ev = [e for e in out.evidence if e.kind == "trend"]
    assert ev and "trending on X (best rank #4, 6 hour(s)" in ev[0].detail
    assert "trending on X" in out.summary


@needs_db
async def test_x_trends_polled_and_kept_when_pages_fail(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth

    now = datetime.now(UTC)
    page = _t24([(now - timedelta(minutes=30), ["#MooDeng", "Hamas"])])
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
    x = next(s for s in idx.sources if s.source == "x_trends")
    assert x.status == "ok" and x.terms == 2 and x.detail and "4 of 5" in x.detail
    hits = idx.match("moo deng", "name")
    assert hits and hits[0].term.source == "x_trends" and hits[0].term.rank == 1
    # every page down on the next poll: the stored topics are used, marked stale
    await db.execute(
        "update lookup_cache set fetched_at = now() - interval '30 minutes' where key=$1",
        fulldepth.XTRENDS_KEY,
    )
    fulldepth._trend_cache = None
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://trends24.in/").mock(return_value=httpx.Response(503))
        router.get(url__startswith="https://trends.google.com/").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            idx = await fulldepth.trend_index(db, http)
    x = next(s for s in idx.sources if s.source == "x_trends")
    assert x.status == "stale" and x.terms == 2
    assert idx.match("moo deng", "name")


# ----------------------------------------------------------------- Bluesky


def _post(text: str, author: str, ago_h: float, likes: int = 0) -> dict:
    at = datetime.now(UTC) - timedelta(hours=ago_h)
    return {
        "text": text,
        "created_at": at.isoformat().replace("+00:00", "Z"),
        "likes": likes,
        "reposts": 0,
        "author": author,
        "uri": None,
    }


def test_bluesky_parse() -> None:
    j = {
        "posts": [
            {
                "uri": "at://did:plc:x/app.bsky.feed.post/1",
                "author": {"handle": "a.bsky.social"},
                "record": {"text": "Moo Deng is back", "createdAt": "2026-10-07T20:20:31.840Z"},
                "likeCount": 7,
                "repostCount": 2,
            },
            "junk",
        ]
    }
    posts = bluesky.parse(j)
    assert len(posts) == 1 and posts[0].likes == 7 and posts[0].author == "a.bsky.social"
    assert bluesky.created({"created_at": posts[0].created_at}) == datetime(
        2026, 10, 7, 20, 20, 31, 840000, tzinfo=UTC
    )


def test_bluesky_relevant_drops_coin_chatter() -> None:
    posts = [
        _post("Moo Deng at the zoo today", "a", 1),
        _post("$MOODENG to the moon, buy on pump.fun", "b", 1),
        _post("moo deng price chart", "c", 1),
        _post("I love $MDW", "d", 1),
        _post("moodeng", "e", 1),
    ]
    rel = bluesky.relevant(posts, "Moo Deng", "MDW")
    assert [p["author"] for p in rel] == ["a"]


def test_bluesky_hit_needs_three_accounts_in_a_day() -> None:
    few = [_post("moo deng", "a", 1), _post("moo deng", "a", 2), _post("moo deng", "b", 3)]
    assert trends.bluesky_hit("Moo Deng", few) is None
    old = [_post("moo deng", a, 30) for a in "abcd"]
    assert trends.bluesky_hit("Moo Deng", old) is None
    posts = [_post("moo deng", a, 1 + i, likes=i) for i, a in enumerate("abcdefghijkl")]
    posts[3]["text"] = "Moo Deng   biting again"
    posts[3]["likes"] = 99
    h = trends.bluesky_hit("Moo Deng", posts)
    assert h is not None and h.term.source == "bluesky" and h.term.views == 12
    assert h.headline == "Moo Deng biting again"
    assert trends.score(h) == 0.48
    ev = trends.bluesky_evidence(h)
    assert ev[0].label == "news_event" and "12 Bluesky post(s)" in ev[0].detail
    assert ev[1].referent is not None and ev[1].referent.source == "bsky:moo deng"


@needs_db
async def test_bluesky_down_serves_cached_posts_as_stale(db: asyncpg.Connection) -> None:
    from tokensage import fulldepth

    await db.execute(
        """insert into lookup_cache (key, value, fetched_at)
           values ('bsky:q:moo deng', $1, now() - interval '3 hours')""",
        [_post("Moo Deng at the zoo", "a", 4)],
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.bsky.app/").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as http:
            found = await fulldepth.bsky_for(db, http, "Moo Deng")
    assert found is not None and found.stale and len(found.posts) == 1
    assert datetime.now(UTC) - found.as_of > timedelta(hours=2)
    # a fresh answer is cached and not stale
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.bsky.app/").mock(
            return_value=httpx.Response(200, json={"posts": []})
        )
        async with httpx.AsyncClient() as http:
            found = await fulldepth.bsky_for(db, http, "Moo Deng")
    assert found is not None and not found.stale and found.posts == []
