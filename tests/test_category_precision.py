"""Category precision and recall rules (TrenchScanner brief 2026-10-07, section 4): the
dictionary lists' usual senses, the second-signal rule for one everyday word, news that needs a
date or a trend, description nouns, compound mascot words, and agreement-based confidence."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from tokensage.engine import trends
from tokensage.engine.context import Ev
from tokensage.engine.knowledge import Entity, load_knowledge
from tokensage.engine.pipeline import EngineInput, _news_needs_a_date, run_basic, run_full

WHEN = datetime(2026, 10, 7, 19, 30, tzinfo=UTC)


def _run(name: str, symbol: str | None = None, description: str | None = None):  # type: ignore[no-untyped-def]
    return run_basic(EngineInput("M", name, symbol or name.upper(), description, None, WHEN))


def _cats(name: str, symbol: str | None = None, description: str | None = None) -> dict[str, float]:
    return dict(_run(name, symbol, description).agg.categories)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Cryptographic World Computer", "animal"),  # "world" is an animal only to WordNet
        ("GOOP HEAD", "animal"),  # "head" of cattle
        ("Right Timeline", "food_object_abstract"),  # "right" as a body part
        ("Faupad", "food_object_abstract"),  # "pad" as a body part
        ("SKIDMARK", "animal"),
        ("KOBE", "animal"),
        ("PLACED", "animal"),
        ("cowboy", "animal"),  # a dictionary compound is not split into "cow"
        ("category", "animal"),
    ],
)
def test_rare_dictionary_senses_do_not_make_a_category(name: str, label: str) -> None:
    assert label not in _cats(name)


@pytest.mark.parametrize("name", ["GAME", "Playground", "BACK", "BOOT", "SPAWN", "Speed", "Agent"])
def test_one_everyday_word_alone_carries_no_celebrity_or_ai(name: str) -> None:
    cats = _cats(name)
    assert not any(c.split("/")[0] in ("celebrity", "ai_agent") for c in cats), cats


def test_a_second_signal_restores_the_everyday_word() -> None:
    # the same word in another input
    assert "ai_agent" in _cats("Agent", "AGENT", "your new portfolio agent")
    # another word for the same label
    assert "ai_agent" in _cats("Agent", "AGENT", "an AI that trades for you")
    # a famous name needs nothing else
    assert "political" in _cats("Trump", "TRUMP")


def test_news_event_needs_a_trend_or_a_dated_event() -> None:
    for name in ("Halloween", "Happy Halloween", "Super Bowl Sunday", "Luigi Mangione"):
        assert "news_event" not in _cats(name), name
    k = load_knowledge()
    idx = trends.TrendIndex([trends.TrendTerm("Halloween", 12.0, 90000)], k)
    out = run_full(EngineInput("M", "Halloween", "SPOOKY", None, None, WHEN, trend_index=idx))
    assert "news_event" in dict(out.agg.categories)


def test_a_dated_lexicon_event_is_news_near_its_date() -> None:
    k = load_knowledge()
    dated = Entity(
        "Big Fight",
        "event",
        ("big fight",),
        ("news_event",),
        "a fight",
        0.5,
        event_date=date(2026, 10, 1),
    )
    kk = dataclasses.replace(k, entities=[*k.entities, dated])
    ev = Ev("entity", "news_event", 0.6, "d", "entities:Big Fight")
    assert _news_needs_a_date([ev], kk, WHEN) == [ev]
    assert _news_needs_a_date([ev], kk, datetime(2026, 12, 1, tzinfo=UTC)) == []
    undated = Ev("entity", "news_event", 0.6, "d", "entities:Halloween")
    assert _news_needs_a_date([undated], kk, WHEN) == []


def test_description_nouns_do_not_start_food_object_abstract() -> None:
    cats = _cats("Hysplex", "HYSPLEX", "Launch a token from Telegram. Five gates, each held.")
    assert "food_object_abstract" not in cats
    # the description's emoji are description evidence, not the name's
    out = _run("StickFrag", "STICKFRAG", "🎮 Fast fights 🔥 Become the last one standing")
    assert all(e.where == "description" for e in out.evidence if e.kind == "emoji")


def test_food_from_dictionary_words_alone_stays_below_half() -> None:
    cats = _cats("beer", "BEER")
    assert 0.2 <= cats["food_object_abstract"] < 0.5


def test_a_name_read_from_the_end_of_another_word_does_not_count() -> None:
    out = _run("Niggy", "NIGGY")
    assert out.agg.referent is None or "Iggy" not in out.agg.referent.label
    assert "humor_crude_offensive" in dict(out.agg.categories)
    assert "political" in _cats("iTrump", "ITRUMP")  # a leading "i" still leaves the name


@pytest.mark.parametrize(
    ("name", "label"),
    [("nintendoge", "animal/dog"), ("FROGMAN", "animal/frog"), ("Catler", "animal/cat")],
)
def test_mascot_words_fused_into_a_name_are_found(name: str, label: str) -> None:
    assert label in _cats(name)


def test_plural_names_match_their_singular() -> None:
    assert "meme_template/pepe_wojak_chad" in _cats("Teenage Mutant Ninja Pepes", "TMNP")


def test_crypto_native_comes_from_the_coins_own_content() -> None:
    assert "crypto_native/utility_claim" in _cats("netrun.fun", "NETRUN")
    assert "crypto_native" in _cats("zCash", "ZCASH")
    assert "crypto_native" in _cats("Faupad", "FAUP")
    # a chain or a GPU named in the description is not a stock coin
    cats = _cats("Potlings", "POTLINGS", "Free mint NFT on Robinhood Chain, 5,000 plants")
    assert not any(c.startswith("tradfi") for c in cats) and "crypto_native" in cats


def test_crypto_native_lands_on_a_specific_sub_label() -> None:
    # every crypto_native rule names a child, so the parent always says which kind it is
    from tokensage.engine.render_summary import _CRYPTO_PHRASE

    data = Path(__file__).resolve().parents[1] / "data"
    taxonomy = yaml.safe_load((data / "taxonomy.yaml").read_text())
    labels = {c["label"] for c in taxonomy["categories"]}
    children = {lbl for lbl in labels if lbl.startswith("crypto_native/")}
    assert children - {"crypto_native/paired_ecosystem"} == set(_CRYPTO_PHRASE)
    for f in ("slang.yaml", "entities_seed.yaml", "stocks.yaml", "known_coins_seed.yaml"):
        assert "[crypto_native]" not in (data / f).read_text(), f
        assert "crypto_native," not in (data / f).read_text(), f
    assert "crypto_native/person" in _cats("Vitalik Fudderin", "VITALIK")
    assert "crypto_native/slang" not in _cats("Vitalik Fudderin", "VITALIK")
    assert "crypto_native/chain_or_coin" in _cats("Soltag", "SOLTAG")
    assert "crypto_native/trading" in _cats("2D Hedge Fund", "2DHF", "Trading desk for traders")
    assert "crypto_native/launchpad" in _cats("EsportPad", "ESPAD")
    assert "crypto_native/company" in _cats("Coinbase", "COIN")
    assert "crypto_native/slang" in _cats("Launch Ape Rug Profit", "LARP")


def test_crypto_native_summary_names_the_kind() -> None:
    # (zCash resolves to the Zcash entity since rules 0.17.0; WBTC is a slang-only chain coin)
    assert "a coin about a blockchain or established coin" in _run("WBTC", "WBTC").summary
    assert "a launchpad / launch-platform coin" in _run("VOLUMEPAD", "VOLUMEPAD").summary


def test_wikipedia_crypto_descriptions_pick_a_sub_label() -> None:
    from tokensage.engine.wikiclass import classify

    assert classify("Canadian programmer, co-founder of Ethereum") == (
        "person",
        ["celebrity/other", "crypto_native/person"],
    )
    assert classify("American cryptocurrency exchange") == ("concept", ["crypto_native/company"])
    assert classify("privacy-focused cryptocurrency") == ("coin", ["crypto_native/chain_or_coin"])
    assert classify("American actress (born 1997)") == ("person", ["celebrity/other"])


def test_two_agreeing_inputs_read_higher_than_one() -> None:
    one = _run("Dog Think", "DTHINK")
    two = _run("Dog Think", "DTHINK", "What do dogs think about? A dog coin.")
    c1, c2 = dict(one.agg.categories)["animal/dog"], dict(two.agg.categories)["animal/dog"]
    assert c2 > c1
    assert one.agg.inputs["animal/dog"] == ["name"]
    assert set(two.agg.inputs["animal/dog"]) == {"name", "description"}
    # a ticker that spells the name is the name again, not a second input
    same = _run("Unc Cat", "UNCCAT")
    assert same.agg.inputs["animal/cat"] == ["name"]


def _audit(path: Path) -> tuple[float, float]:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from category_audit import load, score

    s = score(load(path), 0.2)
    return s["micro_precision"], s["micro_recall"]


@pytest.mark.parametrize(
    ("file", "precision", "recall"),
    [("category_audit.yaml", 0.82, 0.68), ("category_audit_holdout.yaml", 0.8, 0.55)],
)
def test_category_audit_does_not_regress(file: str, precision: float, recall: float) -> None:
    """Hand-labelled pump.fun launches (2026-10-07). Before this pass: 0.66 / 0.49 on the
    tuning set, 0.61 / 0.47 on the blind hold-out."""
    p, r = _audit(Path(__file__).parent / "golden" / file)
    assert p >= precision and r >= recall, (p, r)


def test_audit_files_use_taxonomy_labels() -> None:
    taxonomy = Path(__file__).parents[1] / "data" / "taxonomy.yaml"
    tops = {
        c["label"].split("/")[0] for c in yaml.safe_load(taxonomy.read_text("utf-8"))["categories"]
    }
    for file in ("category_audit.yaml", "category_audit_holdout.yaml"):
        coins = yaml.safe_load((Path(__file__).parent / "golden" / file).read_text("utf-8"))
        for c in coins["coins"]:
            assert set(c["labels"]) <= tops, c
