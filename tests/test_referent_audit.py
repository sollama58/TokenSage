"""The referent regression set (tests/golden/referent_audit.yaml): reads the 2026-10-12 hand
audit judged wrong or too generic, rebuilt as inputs, plus controls it judged right. No
network, no database. `python -m tests.test_referent_audit` prints the pass count."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from tokensage.engine import gazetteer, pairing, trends, wikilookup
from tokensage.engine.knowledge import KnownCoin, load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.pipeline import DbContext, EngineInput, EngineOutput, run_basic, run_full
from tokensage.sources.wikipedia import WikiPage
from tokensage.sources.x import TweetData

CASES: list[dict[str, Any]] = yaml.safe_load(
    (Path(__file__).parent / "golden" / "referent_audit.yaml").read_text("utf-8")
)["cases"]
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
MINT = "So11111111111111111111111111111111111111112"


def _tweet(t: dict[str, Any], tid: str) -> TweetData:
    quoted = _tweet(t["quoted"], tid + "1") if t.get("quoted") else None
    return TweetData(
        id=tid,
        status="ok",
        source="fxtwitter",
        text=t.get("text"),
        created_at=NOW - timedelta(seconds=float(t.get("posted_s_before", 60))),
        author_handle=t.get("author"),
        author_name=t.get("author_name"),
        followers=t.get("followers"),
        verified_type=t.get("verified"),
        author_joined=NOW - timedelta(days=900),
        quoted=quoted,
        quoted_tweet_id=quoted.id if quoted else None,
    )


def _coins(case: dict[str, Any]) -> list[KnownCoin]:
    return [
        KnownCoin(
            symbol=c["symbol"],
            name=c["name"],
            aliases=(),
            chain="solana",
            lore="",
            categories=tuple(c.get("categories") or []),
            referent_label=c["label"],
            referent_kind=c.get("kind", "coin"),
            referent_desc=c.get("desc", ""),
            source="coingecko",
        )
        for c in case.get("extra_coins") or []
    ]


def _wiki(case: dict[str, Any], tweet: TweetData | None) -> list[wikilookup.WikiRef]:
    """What the analyzer's Wikipedia lookup would hand the engine: the spans it asks for,
    each answered by the case's page for that span (if any) through wikilookup.pick."""
    pages = case.get("wiki") or {}
    if not pages:
        return []
    k = load_knowledge()
    n = normalize(case.get("name"), case.get("symbol"), case.get("description"))
    texts = [t.text or "" for t in (tweet, tweet.quoted if tweet else None) if t is not None]
    refs: list[wikilookup.WikiRef] = []
    for span in wikilookup.spans(n, texts, k, gazetteer.packaged()):
        p = pages.get(span.text)
        if p is None:
            continue
        ref = wikilookup.pick(span, [WikiPage(p["title"], p["desc"], None, 1)])
        if ref is not None:
            refs.append(ref)
    return refs


def run_case(case: dict[str, Any]) -> EngineOutput:
    tweet = _tweet(case["x"], "2100000000000000001") if case.get("x") else None
    idx = None
    if "trending" in case:
        idx = trends.TrendIndex(
            [
                trends.TrendTerm(
                    t["term"],
                    float(t.get("spike", 0.0)),
                    int(t.get("views", 1)),
                    source=t.get("source", "wikipedia"),
                )
                for t in case["trending"] or []
            ],
            load_knowledge(),
        )
    pair = None
    if case.get("pair"):
        p = case["pair"]
        pair = pairing.PairInput(
            mint=p["mint"], symbol=p.get("symbol"), name=p.get("name"), kind=p.get("kind", "token")
        )
    inp = EngineInput(
        mint=MINT,
        name=case.get("name"),
        symbol=case.get("symbol"),
        description=case.get("description"),
        image_bytes=None,
        created_at=NOW,
        ctx=DbContext(extra_coins=_coins(case)),
        pair=pair,
        x_kind="tweet" if tweet else None,
        x_url_handle=tweet.author_handle if tweet else None,
        tweet=tweet,
        ocr_lines=[],
        trend_index=idx,
        wiki_refs=_wiki(case, tweet),
    )
    full = bool(tweet or case.get("wiki") or idx is not None or pair is not None)
    return run_full(inp) if full else run_basic(inp)


def check(case: dict[str, Any], out: EngineOutput) -> list[str]:
    """What the read gets wrong against the case's expectations (empty: it passes)."""
    e = case.get("expect") or {}
    rr = out.referent_read
    label = (rr.label if rr else "").casefold()
    desc = ((rr.desc or "") if rr else "").casefold()
    cats = {lbl for lbl, _ in out.agg.categories}
    bad: list[str] = []
    if "referent" in e and (rr is None or e["referent"].casefold() not in label):
        bad.append(f"referent should contain {e['referent']!r}")
    for x in e.get("referent_not", []):
        if rr is not None and x.casefold() in label:
            bad.append(f"referent should not be {x!r}")
    for x in e.get("label_not", []):
        if label == x.casefold():
            bad.append(f"referent should not be {x!r}")
    for x in e.get("desc_not", []):
        if x.casefold() in desc:
            bad.append(f"desc should not say {x!r}")
    if "referent_source" in e and (rr is None or rr.source != e["referent_source"]):
        bad.append(f"source should be {e['referent_source']!r}")
    if e.get("specific") and (rr is None or rr.generic):
        bad.append("referent should be specific")
    if e.get("referent_null") and rr is not None:
        bad.append("referent should be null")
    for c in e.get("categories_exclude", []):
        if c in cats:
            bad.append(f"category {c} should be absent")
    for c in e.get("categories_include", []):
        if c not in cats:
            bad.append(f"category {c} missing")
    return bad


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_referent_audit(case: dict[str, Any]) -> None:
    out = run_case(case)
    bad = check(case, out)
    rr = out.referent_read
    assert not bad, (
        f"{case['audit']} ({case['pattern']}): {bad}\n  referent={rr}\n"
        f"  categories={out.agg.categories}"
    )


def test_set_covers_every_failure_pattern() -> None:
    assert len(CASES) >= 40
    patterns = {c["pattern"] for c in CASES}
    for p in ("post_subject", "news_timing", "homonym", "substring", "incidental", "category"):
        assert p in patterns, p
    assert len({c["id"] for c in CASES}) == len(CASES)


if __name__ == "__main__":  # pragma: no cover - a report for humans
    passed = 0
    for c in CASES:
        res = check(c, run_case(c))
        passed += not res
        rr = run_case(c).referent_read
        print(f"{'ok  ' if not res else 'FAIL'} {c['id']:32} {rr.label if rr else None!r} {res}")
    print(f"{passed}/{len(CASES)} pass")
