"""Audit 0.28, text group: ticker affixes, everyday-word known-coin matches, normalization
(digits, camelCase, zero-width, homoglyphs), marker-only derivatives, pair-token referents,
referent bands and the inherited news_event gate."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from tokensage.engine import lineage, wikiclass
from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import KnownCoin, load_knowledge
from tokensage.engine.normalize import _split_camel, normalize, ticker_base
from tokensage.engine.pairing import PairInput
from tokensage.engine.pipeline import (
    DbContext,
    EngineInput,
    SameNameToken,
    _news_needs_a_date,
    _prune_chain_alias,
    run_basic,
)
from tokensage.engine.ticker import explain

WHEN = datetime(2026, 10, 8, tzinfo=UTC)
K = load_knowledge()


def _run(name: str, symbol: str, description: str | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    return run_basic(EngineInput("M", name, symbol, description, None, WHEN, **kw))


def _coin(sym: str, name: str) -> KnownCoin:
    return KnownCoin(
        symbol=sym,
        name=name,
        aliases=(name.lower(),),
        chain="solana",
        lore=f"{name} lore",
        categories=("crypto_native/chain_or_coin",),
        referent_label=name,
        referent_kind="coin",
        referent_desc=f"{name} desc",
        source="db",
        mint="x" * 44,
    )


# ----------------------------------------------------------------- ET-1 / ET-2


def test_an_english_word_ticker_keeps_its_affix() -> None:
    for t in ("BEAR", "BLINK", "APEX", "NEWTON", "BOOK", "HOTDOG", "BONSAI"):
        assert ticker_base(t, K) == (t, []), t
    # genuine affixes still strip
    assert ticker_base("BPNUT", K)[0] == "PNUT"
    assert ticker_base("PNUT2", K)[0] == "PNUT"
    assert ticker_base("REALPEPE", K)[0] == "PEPE"
    assert ticker_base("BABYDOGEINU", K)[0] == "DOGE"


def test_a_word_ticker_does_not_build_on_the_coin_hidden_inside_it() -> None:
    ctx = DbContext(extra_coins=[_coin("EAR", "THE EAR STAYS ON"), _coin("LINK", "Chainlink")])
    for name, sym in (("Bear", "BEAR"), ("Blink", "BLINK")):
        out = _run(name, sym, ctx=ctx)
        assert out.copy_of == [] and out.agg.referent is None, name
        assert "references_known_coin" not in {f.code for f in out.flags}


def test_an_affix_stripped_ticker_alone_is_not_a_copy() -> None:
    out = _run("Zorp", "BPNUT")
    assert out.copy_of == [] and out.agg.referent is None
    # with the name agreeing it still is
    assert [c["ticker"] for c in _run("Baby PNUT", "BPNUT").copy_of] == ["PNUT"]


def test_the_full_ticker_explains_the_name_before_its_base() -> None:
    for name, sym in (
        ("Bear", "BEAR"),
        ("Apex", "APEX"),
        ("Hotdog", "HOTDOG"),
        ("Bitcoin", "BITCOIN"),
    ):
        e = explain(normalize(name, sym, None), K)
        assert e.method == "equals_token" and e.text == f"${sym} is the name itself", (name, e)
    assert explain(normalize("Jeo Boden", "BODEN", None), K).method == "known_coin"


# ----------------------------------------------------------------- ET-3 / EO-8


def test_an_everyday_word_modifier_does_not_name_a_known_coin() -> None:
    for name, sym in (
        ("House Cat", "HCAT"),
        ("Mother Earth", "EARTH"),
        ("Daddy Issues", "DADDYI"),
        ("Mother of Dragons", "MOD"),
        ("Conor McGregor Pump", "CMP"),
        ("Andy Warhol", "WARHOL"),
        ("Peanut Butter", "PBUT"),
    ):
        out = _run(name, sym)
        assert out.copy_of == [], (name, out.copy_of)
        assert "references_known_coin" not in {f.code for f in out.flags}, name
    out = _run("Mother of Doge", "MOD")
    assert [c["ticker"] for c in out.copy_of] == ["DOGE"]
    assert "celebrity/musician" not in dict(out.agg.categories)


def test_the_same_word_as_the_head_of_the_name_still_names_the_coin() -> None:
    for name, sym, parent in (
        ("Peanut Army", "PARMY", "PNUT"),
        ("Not Peanut", "NOTPNUT", "PNUT"),
        ("Justice for Peanut", "JFP", "PNUT"),
        ("Bonk Army", "BARMY", "BONK"),
        ("Andy X", "ANDYX", "ANDY"),
        ("Baby Pump", "BPUMP", "PUMP"),
        ("Trump wif Hat", "TWH", "TRUMP"),
    ):
        assert parent in [c["ticker"] for c in _run(name, sym).copy_of], name


# ----------------------------------------------------------------- ET-4


def test_a_coin_surface_keeps_its_dictionary_sense_in_a_longer_name() -> None:
    assert "animal/other" in dict(_run("Goat Farm", "GOATF").agg.categories)
    assert "animal/cat" in dict(_run("Kitty Litter", "KLIT").agg.categories)


# ----------------------------------------------------------------- ET-5 / ET-6 / ET-7 / ET-8


def test_digit_runs_are_not_squeezed() -> None:
    n = normalize("1000x Pepe", "1000X", None)
    assert n.name_tokens == ["1000x", "pepe"] and n.obfuscation == []
    assert normalize("Pepe 2000", "PEPE", None).name_tokens == ["pepe", "2000"]
    assert normalize("moooon", "MOON", None).name_tokens == ["moon"]  # letters still are


def test_camel_case_keeps_a_lone_capital() -> None:
    assert _split_camel("DogeX") == "Doge X"
    assert _split_camel("XDoge") == "X Doge"
    assert _split_camel("PepeV2") == "Pepe V 2"
    assert _split_camel("AIAgentSupercycle") == "AI Agent Supercycle"
    assert _split_camel("TSLAx") == "TSLAx"
    n = normalize("DogeX", "DOGEX", None)
    assert n.name_tokens == ["doge", "x"] and n.name_compact == "dogex"
    assert lineage.match_inputs(n, "Doge", "DOGE") == []
    out = _run("PepeV2", "PEPEV2")
    assert [m.code for m in out.normalized.markers] == ["version:2"]
    assert "derivative/sequel" in dict(out.agg.categories)


def test_the_ai_suffix_marker_survives_trailing_emoji_and_punctuation() -> None:
    for name in ("Pepe AI ", "Pepe AI 🚀", "Pepe Agent!"):
        out = _run(name, "PEPEAI")
        assert "marker:ai-suffix" in {m.code for m in out.normalized.markers}, name
        assert "derivative/template_family" in dict(out.agg.categories), name


def test_a_zero_width_joiner_is_zero_width_not_a_homoglyph() -> None:
    out = _run("Pe‍pe", "PEPE")
    assert out.normalized.obfuscation == ["zero_width"]
    flags = {f.code for f in out.flags}
    assert "obfuscated_text" in flags and "homoglyph_ticker" not in flags
    assert "derivative/homoglyph_spoof" not in dict(out.agg.categories)
    assert normalize("Frog \U0001f438‍\U0001f680", "FROG", None).obfuscation == []


# ----------------------------------------------------------------- ET-9 / EC-1


def test_a_homoglyph_spoof_is_not_a_cyrillic_name() -> None:
    out = _run("Pеpe", "PEPE")  # one Cyrillic е
    assert out.normalized.obfuscation == ["homoglyph"] and out.normalized.scripts == []
    assert "regional_language" not in dict(out.agg.categories)
    assert "regional_script" not in {f.code for f in out.flags}
    assert "derivative/homoglyph_spoof" in dict(out.agg.categories)


def test_bilingual_names_are_not_homoglyph_spoofs() -> None:
    for name, sym in (
        ("柴犬コイン", "SHIBA"),
        ("Pepe 日本", "PEPE"),
        ("강아지 coin", "DOG"),
        ("Pepe Собака", "PEPE"),
        ("Пепе Coin", "PEPE"),
    ):
        out = _run(name, sym)
        assert "homoglyph" not in out.normalized.obfuscation, name
        assert "homoglyph_ticker" not in {f.code for f in out.flags}, name
        assert "derivative/homoglyph_spoof" not in dict(out.agg.categories), name
    assert normalize("Pepe Собака", "PEPE", None).name_tokens == ["pepe", "sobaka"]
    assert normalize("Пепе Coin", "PEPE", None).scripts == ["Cyrillic"]
    # a mixed word is still a spoof
    assert normalize("Рepe", "PEPE", None).obfuscation == ["homoglyph"]
    assert normalize("PEPE Сoin", "PEPE", None).obfuscation == ["homoglyph"]


# ----------------------------------------------------------------- ET-10


def test_a_presidential_election_is_an_event() -> None:
    assert wikiclass.classify("2024 United States presidential election") == (
        "event",
        ["news_event"],
    )
    assert wikiclass.classify("45th president of the United States") == ("person", ["political"])
    assert wikiclass.classify("presidential candidate") == ("person", ["political"])


# ----------------------------------------------------------------- EO-1


def _doggo_pair() -> PairInput:
    return PairInput(
        mint="PAIRMINT",
        symbol="DOGGO",
        name="Doggo",
        kind="token",
        source="analysis",
        pumpfun=True,
        referent=ReferentCandidate(
            "dog",
            "animal",
            "an animal coin by its name; nothing more specific identified",
            "analysis:PAIRMINT",
            0.47,
            generic=True,
        ),
        categories=[("animal/dog", 0.6), ("animal", 0.6)],
    )


def test_a_generic_pair_referent_stays_generic() -> None:
    out = _run("Baby Doggo", "BDOGGO", pair=_doggo_pair())
    rr = out.referent_read
    assert rr is not None and rr.generic and rr.label == "dog" and rr.confidence <= 0.49
    assert "about dog" not in out.summary and "may refer to dog" not in out.summary
    assert "is itself a pump.fun coin that reads as an animal-mascot coin" in out.summary
    out = _run("Zork", "ZORK", pair=_doggo_pair())
    assert out.referent_read is None and "about dog" not in out.summary
    assert "reads as an animal-mascot coin" in out.summary


def test_stored_generic_flag_is_read_from_the_pair_analysis() -> None:
    from tokensage.resolve import pair as resolve_pair

    class Conn:
        async def fetchval(self, *_: Any) -> dict[str, Any]:
            return {
                "raw": {"name": "Doggo", "symbol": "DOGGO"},
                "launchpad": "pump.fun",
                "referent": {"label": "dog", "kind": "animal", "confidence": 0.47, "generic": True},
                "categories": [{"label": "animal/dog", "confidence": 0.6}],
            }

    pi = PairInput(mint="PAIRMINT", kind="token")
    asyncio.run(resolve_pair._from_analysis(Conn(), pi))  # type: ignore[arg-type]
    assert pi.referent is not None and pi.referent.generic


# ----------------------------------------------------------------- EO-2


def test_a_squashed_named_guess_with_no_theme_is_no_referent() -> None:
    for name in ("Speed", "Drake", "Einstein"):
        out = _run(name, name.upper())
        assert out.agg.referent is not None and out.agg.referent.score < 0.3, name
        assert out.referent_read is None, name
        assert "referent is a weak guess" not in out.caveats, name
        assert "no clear reference found" in out.summary, name


# ----------------------------------------------------------------- EO-3


def test_a_marker_with_nothing_to_derive_from_is_not_a_derivative() -> None:
    for name, sym in (
        ("Real Madrid", "REALMADRID"),
        ("Real Estate", "RESTATE"),
        ("Classic Rock", "CROCK"),
        ("World War II", "WW2"),
        ("Secret Agent", "SAGENT"),
        ("PLAGUE II", "PLAGUE"),
    ):
        out = _run(name, sym)
        assert not any(lbl.startswith("derivative") for lbl, _ in out.agg.categories), name
        assert out.agg.main is None or out.agg.main[0] != "derivative", name
    # a known parent keeps the marker's nuance
    assert "derivative/copycat" in dict(_run("Real Fartcoin", "FARTCOIN").agg.categories)
    assert "derivative/sequel" in dict(_run("m00 deng classic", "MOODENG").agg.categories)


def test_a_recent_namesake_keeps_the_marker() -> None:
    earlier = SameNameToken("Q" * 44, "Blorbo", "BLORBO", WHEN - timedelta(days=2), "db")
    out = _run("Real Blorbo", "BLORBO", ctx=DbContext(same_name=[earlier]))
    assert "derivative/copycat" in dict(out.agg.categories)
    assert any(ev.source == "templates:marker:original-claim" for ev in out.evidence)


# ----------------------------------------------------------------- EO-4


def test_a_demoted_description_referent_yields_to_the_names_own() -> None:
    out = _run("Ohio Dog", "ODOG", "Elon Musk Elon Musk elonmusk approved")
    assert out.agg.referent is not None and out.agg.referent.label == "Ohio"
    rr = out.referent_read
    assert rr is not None and rr.label == "Ohio" and not rr.generic and rr.confidence >= 0.5
    assert not any(c.startswith("referent is ambiguous") for c in out.caveats)
    assert "weak guess: Elon Musk" not in out.summary


# ----------------------------------------------------------------- EO-5


def test_a_theme_borrowed_from_the_pair_token_alone_is_context() -> None:
    pair = PairInput(
        mint="PAIR", symbol="PARMY", name="Pepe Army", kind="token", source="onchain", pumpfun=True
    )
    out = _run("Zorbix", "ZORB", pair=pair)
    assert out.referent_read is None
    assert out.summary.startswith(
        "Zorbix ($ZORB) was launched into the $PARMY (Pepe Army) community"
    )
    assert "by its db" not in out.summary
    # building on the pair token makes its theme the coin's
    out = _run("Baby Pepe Army", "BPARMY", pair=pair)
    assert out.referent_read is not None and out.referent_read.label == "Pepe the Frog"


# ----------------------------------------------------------------- EO-6


def test_a_ticker_repeating_a_weak_name_word_does_not_lift_it() -> None:
    out = _run("Speed Demon", "SPEED")
    assert out.agg.categories == [] and out.referent_read is None
    # a non-generic word agreeing with its ticker still counts twice
    assert dict(_run("Secret Agent", "AGENT").agg.categories)["ai_agent"] >= 0.7


# ----------------------------------------------------------------- EC-6


def test_the_sol_suffix_does_not_outrank_the_coins_subject() -> None:
    out = _run("dog on sol", "DOS")
    assert out.agg.main is not None and out.agg.main[0] == "animal"
    assert out.agg.referent is None
    assert "Solana" not in out.summary
    # with nothing else in the name the alias still reads
    out = _run("Zorblax Sol", "ZORB")
    assert out.agg.referent is not None and out.agg.referent.label == "Solana"
    assert _run("Solana Summer", "SOLSUM").agg.referent is not None


def test_sol_from_the_logo_alone_never_counts() -> None:
    ref = ReferentCandidate("Solana", "coin", "solana", "entities:Solana", 0.63, surface="sol")
    rows = [
        Ev(
            "entity",
            "crypto_native/chain_or_coin",
            0.64,
            "d",
            "entities:Solana",
            "image",
            referent=ref,
        ),
        Ev("entity", "referent", 0.63, "d", "entities:Solana", "image", referent=ref),
    ]
    assert _prune_chain_alias(rows) == []


# ----------------------------------------------------------------- TDD-1


def test_an_inherited_known_coin_news_event_needs_a_live_trend() -> None:
    for name, sym in (("Peanut the Squirrel", "PNUT"), ("Hawk Tuah", "HAWK"), ("GameStop", "GME")):
        out = _run(name, sym)
        assert "news_event" not in dict(out.agg.categories), name
    assert _run("GameStop", "GME").agg.main[0] != "news_event"  # type: ignore[index]
    inherited = Ev("known_coin_inherit", "news_event", 0.77, "d", "known_coins:PNUT")
    assert _news_needs_a_date([inherited], K, WHEN) == []
    trend = Ev("trend", "news_event", 0.5, "d", "news:peanut", "trend")
    assert _news_needs_a_date([inherited, trend], K, WHEN) == [inherited, trend]
