"""One-word names and tickers against trending labels (rules 0.18.0): a word of a longer
label, the ticker as the whole label, a one-word news search, and the everyday-word gate."""

from __future__ import annotations

from tokensage.engine import trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.pipeline import EngineInput, run_full


def _x(term: str, rank: int = 5) -> trends.TrendTerm:
    return trends.TrendTerm(term, 0.0, 4, source="x_trends", rank=rank)


def _run(
    name: str, symbol: str, terms: list[trends.TrendTerm], desc: str | None = None
) -> list[trends.TrendHit]:
    idx = trends.TrendIndex(terms, load_knowledge())
    out = run_full(EngineInput("m", name, symbol, desc, None, None, trend_index=idx))
    return out.trend_hits


def test_rare_and_everyday_words() -> None:
    k = load_knowledge()
    for w in ("leoncio", "glasnow", "antonela", "ozzie"):
        assert trends.rare(w, k) and not trends.everyday(w, k), w
    for w in ("west", "moon", "juniper", "chow"):
        assert trends.everyday(w, k) and not trends.rare(w, k), w
    # frequent names and places are not rare, though they are not dictionary words either
    for w in ("quebec", "tyler"):
        assert not trends.rare(w, k) and not trends.everyday(w, k), w


def test_one_word_name_matches_a_word_of_a_trending_label() -> None:
    hits = _run("Leoncio", "LEON", [_x("Leoncio Gomez")])
    assert [(h.term.term, h.partial, h.where) for h in hits] == [("Leoncio Gomez", True, "name")]
    # a whole-label match wins over a word of another label
    hits = _run("Leoncio", "LEON", [_x("Leoncio Gomez"), _x("Leoncio", rank=30)])
    assert [(h.term.term, h.partial) for h in hits] == [("Leoncio", False)]


def test_partial_match_gives_a_weaker_referent() -> None:
    idx = trends.TrendIndex([_x("Leoncio Gomez", rank=3)], load_knowledge())
    out = run_full(EngineInput("m", "Leoncio", "LEON", None, None, None, trend_index=idx))
    refs = [e for e in out.evidence if e.kind == "referent" and e.source.startswith("xtrends:")]
    assert refs and refs[0].referent is not None
    assert refs[0].referent.score == 0.35  # 0.45 for a top-10 topic, less 0.1 for a word
    assert "one word of 'Leoncio Gomez'" in next(
        e.detail for e in out.evidence if e.kind == "trend"
    )


def test_everyday_and_frequent_words_need_a_second_signal() -> None:
    # "West" of "Kanye West", "Tyler" of "Tyler Mahle": not without another agreeing word
    assert _run("WEST", "WEST", [_x("Kanye West")]) == []
    assert _run("Tyler", "TYLER", [_x("Tyler Mahle")]) == []
    hits = _run("WEST", "WEST", [_x("Kanye West")], desc="kanye's new album dropped")
    assert [(h.term.term, h.support) for h in hits] == [("Kanye West", "kanye")]


def test_everyday_x_topic_needs_a_second_signal_but_wikipedia_does_not() -> None:
    assert _run("Chow", "CHOW", [_x("Chow")]) == []
    hits = _run("Halloween", "SPOOKY", [trends.TrendTerm("Halloween", 12.0, 90000)])
    assert [h.term.source for h in hits] == ["wikipedia"]
    hits = _run("Juniper", "JUNI", [trends.TrendTerm("juniper", 0.0, 5000, "google_trends")])
    assert [h.term.term for h in hits] == ["juniper"]


def test_ticker_matches_a_whole_label() -> None:
    hits = _run("Hat Dog", "GLASNOW", [_x("Glasnow")])
    assert [(h.term.term, h.where, h.matched_on) for h in hits] == [("Glasnow", "symbol", "symbol")]
    # an everyday ticker needs a second signal; a ticker spelling the name is the name
    assert _run("Hat Dog", "MOON", [_x("Moon")]) == []
    assert [h.where for h in _run("Glasnow", "GLASNOW", [_x("Glasnow")])] == ["name"]


def test_news_word_and_one_word_news_hit() -> None:
    k = load_knowledge()
    assert trends.news_word("Leoncio", k) == "Leoncio"
    assert trends.news_word("Leoncio 2", k) == "Leoncio"
    for name in ("Juniper", "WEST", "Tyler", "Ozzy", "Moo Deng", "QC4T"):
        assert trends.news_word(name, k) is None, name
    heads = [{"title": "Leoncio scores", "source": s, "published": None} for s in ("A", "B")]
    assert trends.news_hit("Leoncio", heads) is not None
    assert trends.news_hit("Leoncio", heads, min_outlets=trends.MIN_ONE_WORD_OUTLETS) is None
    heads.append({"title": "Leoncio again", "source": "C", "published": None})
    assert trends.news_hit("Leoncio", heads, min_outlets=trends.MIN_ONE_WORD_OUTLETS)


def test_accented_labels_meet_unaccented_names() -> None:
    hits = _run("Leoncio", "LEON", [_x("Leôncio Gomez")])
    assert [(h.term.term, h.partial) for h in hits] == [("Leôncio Gomez", True)]
    hits = _run("Beyonce", "BEY", [trends.TrendTerm("Beyoncé", 0.0, 9000, "google_trends")])
    assert [h.term.term for h in hits] == ["Beyoncé"]


def test_non_latin_labels_do_not_leave_stray_letters() -> None:
    # "Aぇヤンタン" cleaned to "a" matched every coin text with the word "a"
    assert trends.surfaces_for("Aぇヤンタン") == []
    assert _run("a memecoin", "MEME", [_x("Aぇヤンタン")], desc="a coin for a dream") == []


def test_inflections_of_dictionary_words_are_not_rare() -> None:
    k = load_knowledge()
    assert not trends.rare("dumbest", k) and trends.news_word("Dumbest", k) is None
    assert not trends.rare("stories", k)


def test_ordinary_phrases() -> None:
    k = load_knowledge()
    # Bluesky has dozens of posts a day with any of these, trending or not
    for p in ("My Shoe", "Plague Doctor", "life is good", "This Is True", "Peanut the Squirrel"):
        assert trends.ordinary(p, k), p
    for p in ("Moo Deng", "Leoncio Gomez", "Susan Dell", "Claude Opus"):
        assert not trends.ordinary(p, k), p
