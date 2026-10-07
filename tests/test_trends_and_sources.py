"""Trend matching, Wikimedia spike maths, Google News parsing, CoinGecko parsing, OCR."""

from __future__ import annotations

import io
from datetime import date

import httpx
import pytest
import respx

from tokensage.engine import ocr, trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.pipeline import EngineInput, run_full
from tokensage.sources import coingecko, gnews, wikimedia


def test_surfaces_for_titles() -> None:
    assert trends.surfaces_for("Peanut (squirrel)") == [
        "peanut squirrel",
        "peanut  squirrel",
        "peanut",
    ] or (
        "peanut squirrel" in trends.surfaces_for("Peanut (squirrel)")
        and "peanut" in trends.surfaces_for("Peanut (squirrel)")
    )
    assert trends.surfaces_for("List of Marvel films") == []
    assert trends.surfaces_for("Special:Search") == []
    assert "moo deng" in trends.surfaces_for("Moo Deng")


def test_trend_index_matches_and_weights() -> None:
    k = load_knowledge()
    idx = trends.TrendIndex(
        [
            trends.TrendTerm("Peanut (squirrel)", 42.0, 900_000),
            trends.TrendTerm("Donald Trump", 2.5, 1_500_000),
            trends.TrendTerm("Quokka", 3.5, 120_000),
        ],
        k,
    )
    hits = idx.match("peanut the squirrel 2 0", "name")
    assert hits and hits[0].term.term == "Peanut (squirrel)"
    evs = trends.evidence(hits, idx)
    assert evs and evs[0].weight == 0.6 and evs[0].label == "news_event"
    generic = trends.evidence(idx.match("donald trump coin", "name"), idx)
    assert generic and generic[0].weight <= 0.15  # perennial entity: damped
    assert idx.match("nothing here", "name") == []


def test_run_full_uses_trend_index_without_network() -> None:
    k = load_knowledge()
    idx = trends.TrendIndex([trends.TrendTerm("Quokka", 12.0, 300_000)], k)
    out = run_full(EngineInput("m", "Quokka Coin", "QUOK", None, None, None, trend_index=idx))
    assert out.trend_hits and out.trend_hits[0].term.term == "Quokka"
    assert dict(out.agg.categories).get("news_event", 0) >= 0.5
    assert out.depth == "full"


def test_spike_ratios() -> None:
    today = {"A": 1000, "B": 500, "C": 100}
    hist = {
        date(2026, 10, 1): {"A": 100, "B": 500, "X": 10},
        date(2026, 10, 2): {"A": 100, "B": 500, "X": 10},
    }
    r = wikimedia.spike_ratios(today, hist)
    assert r["A"] == 10.0 and r["B"] == 1.0
    assert r["C"] == 10.0  # absent before -> compared to that day's floor (10)
    assert wikimedia.spike_ratios(today, {}) == {"A": 1.0, "B": 1.0, "C": 1.0}


@respx.mock
async def test_wikimedia_top_and_404() -> None:
    respx.get(url__regex=r"https://wikimedia\.org/.*/2026/10/04").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "articles": [
                            {"article": "Main_Page", "views": 5_000_000},
                            {"article": "Special:Search", "views": 1_000_000},
                            {"article": "Peanut_(squirrel)", "views": 900_000},
                        ]
                    }
                ]
            },
        )
    )
    respx.get(url__regex=r"https://wikimedia\.org/.*/2026/10/05").mock(
        return_value=httpx.Response(404)
    )
    async with httpx.AsyncClient() as http:
        top = await wikimedia.top_articles(http, date(2026, 10, 4))
        assert top == {"Peanut (squirrel)": 900_000}
        assert await wikimedia.top_articles(http, date(2026, 10, 5)) is None


@respx.mock
async def test_gnews_parse() -> None:
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><title>Peanut the squirrel seized &amp; euthanized - CNN</title>
    <source url="u">CNN</source>
    <pubDate>Fri, 01 Nov 2024 12:00:00 GMT</pubDate></item>
    <item><title><![CDATA[Second story]]></title></item></channel></rss>"""
    respx.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text=rss)
    )
    async with httpx.AsyncClient() as http:
        heads = await gnews.search(http, "peanut squirrel")
    assert heads is not None and len(heads) == 2
    assert heads[0].title.startswith("Peanut the squirrel seized &") and heads[0].source == "CNN"
    assert heads[1].title == "Second story"


@respx.mock
async def test_coingecko_markets_parse() -> None:
    respx.get(url__startswith=f"{coingecko.BASE}/coins/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "dogwifcoin",
                    "symbol": "wif",
                    "name": "dogwifhat",
                    "image": "https://i/x.png",
                    "market_cap_rank": 50,
                },
                {"id": "", "symbol": "x", "name": "bad"},
            ],
        )
    )
    async with httpx.AsyncClient() as http:
        coins = await coingecko.category_markets(http, "", "politifi")
    assert coins is not None and len(coins) == 1
    assert coins[0].symbol == "WIF" and coins[0].categories == ["political"]


@pytest.mark.skipif(not ocr.available(), reason="rapidocr not installed")
def test_ocr_reads_ticker_and_feeds_engine() -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (420, 160), (30, 60, 120))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    d.text((30, 40), "$PNUT", fill=(250, 220, 40), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    lines, err = ocr.read(buf.getvalue())
    assert err is None and lines and "PNUT" in lines[0].text.upper()

    # a logo that says $PNUT on a token called "Fluffy" with ticker FLUF -> copycat tell
    out = run_full(EngineInput("m", "Fluffy", "FLUF", None, buf.getvalue(), None, run_ocr=True))
    assert any(e.kind == "ocr_other_ticker" for e in out.evidence)
    assert dict(out.agg.categories).get("derivative/logo_reuse", 0) >= 0.5
    assert out.ocr_lines and "PNUT" in out.ocr_lines[0].text.upper()
    assert ocr.read(b"junk")[1] is not None


def test_gnews_name_query() -> None:
    assert gnews.name_query("Le Chonk") == "Le Chonk"
    assert gnews.name_query("Peanut the Squirrel 2.0") == "Peanut the Squirrel"
    assert gnews.name_query("Google Playground") == "Google Playground"
    # one word, or one word plus filler: too generic to search the news for
    assert gnews.name_query("Claudia") is None
    assert gnews.name_query("The 9-5 Coin") is None
    assert gnews.name_query("cat wif") is None
    assert gnews.name_query(None) is None


def test_gnews_relevant_drops_price_pages_and_other_stories() -> None:
    heads = [
        {"title": "Mistral announces Le Chonk, a new open model - DW", "source": "DW"},
        {"title": "Mistral's new 'Le Chonk' AI model is big and open", "source": "Wired"},
        {"title": "Le Chonk price today: CHONK to USD converter", "source": "CoinX"},
        {"title": "Le Chonk (CHONK) jumps 40% on launch day", "source": "Crypto"},
        {"title": "Chonky cats of the week", "source": "Cats"},
    ]
    rel = gnews.relevant(heads, "Le Chonk", "CHONK")
    assert [h["source"] for h in rel] == ["DW", "Wired", "Crypto"]
    # a ticker that is not a word of the name marks a story about the coin itself
    rel = gnews.relevant(
        [{"title": "Quantus jumps 9% as QTC lists cross-chain", "source": "a"}], "Quantus", "QTC"
    )
    assert rel == []


def test_news_hit_needs_two_outlets() -> None:
    one = [{"title": "Le Chonk launched", "source": "DW"}]
    assert trends.news_hit("Le Chonk", one) is None
    assert trends.news_hit("Le Chonk", one * 3) is None  # one outlet, three copies
    two = one + [{"title": "Le Chonk is big", "source": "Wired"}]
    h = trends.news_hit("Le Chonk", two)
    assert h is not None and h.term.source == "news" and h.headline == "Le Chonk launched"


def test_name_in_the_news_raises_news_event() -> None:
    heads = [{"title": f"Le Chonk story {i}", "source": f"outlet{i}"} for i in range(6)]
    hit = trends.news_hit("Le Chonk", heads)
    assert hit is not None
    out = run_full(
        EngineInput(
            mint="m",
            name="Le Chonk",
            symbol="CHONK",
            description=None,
            image_bytes=None,
            created_at=None,
            trend_index=trends.TrendIndex([], load_knowledge()),
            news_hits=[hit],
        )
    )
    assert [h.term.source for h in out.trend_hits] == ["news"]
    assert "news_event" in dict(out.agg.categories)
    assert any(e.kind == "trend" and e.label == "news_event" for e in out.evidence)
