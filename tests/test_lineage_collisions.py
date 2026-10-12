"""Ticker collisions in copy lineage (rules 0.30.0, deep eval 2026-10-12 fix 4).

A shared ticker alone used to make an unrelated earlier coin this one's "original": its
referent and categories leaked in. The fixtures below are written from hand-audited reads
(read-audit/sample*.json, result*.json): the audited coin's name, ticker, description or post,
and the earlier coin TokenSage wrongly took as the original.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tokensage.engine.context import ReferentCandidate
from tokensage.engine.knowledge import KnownCoin, load_knowledge
from tokensage.engine.lineage import name_agrees
from tokensage.engine.normalize import normalize
from tokensage.engine.pipeline import (
    DbContext,
    EngineInput,
    EngineOutput,
    PriorRead,
    SameNameToken,
    run_basic,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
K = load_knowledge()


def _earlier(mint: str, name: str, sym: str, hours: float, source: str = "db") -> SameNameToken:
    return SameNameToken(mint, name, sym, NOW - timedelta(hours=hours), source)


def _gecko(sym: str, name: str, categories: tuple[str, ...]) -> KnownCoin:
    """A CoinGecko coin as the known-coin cron stores it."""
    return KnownCoin(
        symbol=sym,
        name=name,
        aliases=(name.lower(),),
        chain="solana",
        lore=f"{name}: CoinGecko meme coin",
        categories=categories,
        referent_label=name,
        referent_kind="coin",
        referent_desc="CoinGecko meme coin (rank 838)",
        source="coingecko",
    )


def _run(name: str, sym: str, ctx: DbContext, desc: str | None = None) -> EngineOutput:
    return run_basic(EngineInput("mine", name, sym, desc, None, NOW, ctx=ctx))


def _codes(out: EngineOutput) -> set[str]:
    return {f.code for f in out.flags}


def _assert_collision(out: EngineOutput, leaked_referent: str, leaked: set[str]) -> None:
    lin = out.lineage
    assert lin is not None and lin.original is None, lin
    assert not [c for c in out.copy_of if c.get("recent")]  # no copy_of entry for it
    rr = out.referent_read
    assert rr is None or leaked_referent not in rr.label, rr
    assert out.agg.referent is None or leaked_referent not in out.agg.referent.label
    cats = dict(out.agg.categories)
    assert not leaked & set(cats), out.agg.categories
    assert "derivative/copycat" not in cats
    assert not any(e.kind == "copy_inherit" for e in out.evidence)
    assert {"copycat", "earlier_same_name"}.isdisjoint(_codes(out))
    assert "ticker_collision" in _codes(out)


# ----------------------------------------------------------------- name agreement


def test_names_agree_by_spelling_filler_or_a_distinctive_word_not_by_ticker() -> None:
    def agrees(mine: str, theirs: str) -> bool:
        return name_agrees(normalize(mine, "X", None), theirs, K)

    assert agrees("Animal", "Animal Coin")  # filler dropped
    assert agrees("Peanutt", "Peanut")  # close spelling
    assert agrees("Blorbo Frog", "Blorbo")  # a distinctive shared word
    assert agrees("Markodex The Trencher", "Markodex")
    assert agrees("Yuke The Giraffe", "YUKE")
    assert agrees("\U0001f43f️", "\U0001f43f️")  # an emoji name, as written
    # the audited collisions
    assert not agrees("The White Lioness", "Terra")
    assert not agrees("Social Intelligence", "Super Inu")
    assert not agrees("Split Inu", "Stupid Inu")  # "inu" is filler
    assert not agrees("cherryOS", "Justice For Cherry")
    assert not agrees("Pit Fighting", "The Pit")  # "pit" is an everyday word
    assert not agrees("Samsung Ape", "Self-Aware Meme")
    assert not agrees("Quantora Trading", "That's my quant")
    assert not agrees("Relay", "FomoRelay")
    assert not agrees("Anything", "")
    assert not agrees("DogeX", "Doge")  # a fused name is not the word inside it
    assert not agrees("Pepe", "Pepi")  # too short for a fuzzy match


# ----------------------------------------------------------------- the audited patterns


def test_luna_the_viral_lioness_is_not_a_copy_of_terra() -> None:
    terra = PriorRead(
        categories=[("crypto_native", 0.9), ("crypto_native/chain_or_coin", 0.9)],
        referent=ReferentCandidate("Terra (LUNA)", "coin", "collapsed stablecoin chain", "a", 0.8),
    )
    ctx = DbContext(
        same_name=[_earlier("terra", "Terra", "LUNA", 16 * 24, "dexscreener")],
        extra_coins=[_gecko("LUNA", "Luna by Virtuals", ("ai_agent",))],
        originals={"terra": terra},
    )
    out = _run(
        "The White Lioness",
        "LUNA",
        ctx,
        "A super rare white Lioness named Luna has become a viral sensation this morning",
    )
    _assert_collision(out, "Terra", {"crypto_native/chain_or_coin", "ai_agent"})
    rr = out.referent_read
    assert rr is None or "Luna by Virtuals" not in rr.label
    assert "animal" in dict(out.agg.categories)  # its own theme, the lioness
    assert out.lineage is not None and out.lineage.same_ticker == 1
    assert out.lineage.kind == "original"  # 16 d earlier: outside the 24 h rank window
    assert out.lineage.reference is None  # Luna by Virtuals is a collision, not a reference
    assert any("same_ticker" == e.kind for e in out.evidence)


def test_social_intelligence_is_not_super_inu_or_a_dog_coin() -> None:
    ctx = DbContext(
        same_name=[_earlier("stupid", "Stupid Inu", "SI", 9 * 24)],
        extra_coins=[_gecko("SI", "Super Inu", ("animal", "animal/dog"))],
        originals={
            "stupid": PriorRead(
                categories=[("animal", 0.8), ("animal/dog", 0.8)],
                referent=ReferentCandidate("Stupid Inu", "meme", None, "a", 0.7),
            )
        },
    )
    out = _run("Social Intelligence", "SI", ctx)
    _assert_collision(out, "Inu", {"animal", "animal/dog"})
    # the established coin stays a weak, ticker-only reference
    ref = next(c for c in out.copy_of if c["ticker"] == "SI")
    assert ref["recent"] is False and ref["signals"] == ["ticker"]
    row = next(e for e in out.evidence if e.source == "known_coins:SI")
    assert row.referent is None and row.weight <= 0.3 and row.where == "symbol"
    assert "references_known_coin" not in _codes(out)


def test_cherry_os_does_not_inherit_zohran_mamdani_from_justice_for_cherry() -> None:
    prior = PriorRead(
        categories=[("political", 0.8)],
        referent=ReferentCandidate("Zohran Mamdani", "person", "Mayor of New York", "a", 0.8),
    )
    ctx = DbContext(
        same_name=[_earlier("jfc", "Justice For Cherry", "Cherry", 5.3)],
        originals={"jfc": prior},
    )
    out = _run("cherryOS", "CHERRY", ctx, "an AI operating system")
    _assert_collision(out, "Zohran", {"political"})
    lin = out.lineage
    assert lin is not None and lin.same_ticker == 1
    # an earlier $CHERRY in the 24 h rank window, but "cherry" is an everyday word and the
    # names differ: a ticker collision alone does not make it a copy
    assert lin.kind == "original" and lin.original is None


def test_pit_fighting_is_not_pitbull() -> None:
    ctx = DbContext(
        same_name=[_earlier("thepit", "The Pit", "PIT", 3 * 24, "dexscreener")],
        extra_coins=[_gecko("PIT", "Pitbull", ("animal", "animal/dog"))],
        originals={"thepit": PriorRead(categories=[("crypto_native/pumpfun_meta", 0.6)])},
    )
    out = _run(
        "Pit Fighting",
        "PIT",
        ctx,
        "Pit Fighting is live. Ranked top 3 split the $PIT fee reward",
    )
    _assert_collision(out, "Pitbull", {"animal/dog"})
    assert out.lineage is not None and out.lineage.reference is None
    assert out.lineage.kind == "original"


def test_samsung_ape_does_not_copy_self_aware_meme() -> None:
    prior = PriorRead(
        categories=[("meme_template", 0.6), ("meme_template/other", 0.6)],
        referent=ReferentCandidate("self-aware meme", "meme", None, "a", 0.6),
    )
    ctx = DbContext(
        same_name=[_earlier("sam", "Self-Aware Meme", "SAM", 5 * 24)],
        originals={"sam": prior},
    )
    out = _run("Samsung Ape", "SAM", ctx)
    _assert_collision(out, "self-aware", {"meme_template", "meme_template/other"})


def test_a_ticker_collision_sharing_the_logo_is_still_a_copy() -> None:
    from tests.test_lineage import _feats, _logo

    ctx = DbContext(
        same_name=[_earlier("pit", "The Pit", "PIT", 3)],
        image_candidates=[_logo("pit", 3, name="The Pit", sym="PIT")],
    )
    inp = EngineInput("mine", "Pit Fighting", "PIT", None, None, NOW, ctx=ctx)
    inp.logo_features = _feats()
    out = run_basic(inp)
    lin = out.lineage
    assert lin is not None and lin.original is not None and lin.original.mint == "pit"
    assert lin.original.match == ["ticker", "image"]
    assert lin.same_ticker == 0  # it shares the logo: not a collision


def test_a_name_that_agrees_still_copies_and_inherits() -> None:
    prior = PriorRead(
        categories=[("animal", 0.8), ("animal/other", 0.8)],
        referent=ReferentCandidate("animal memes", "meme", None, "a", 0.7),
    )
    ctx = DbContext(
        same_name=[_earlier("orig", "Animal Coin", "ANIMAL", 4 * 24)],
        originals={"orig": prior},
    )
    out = _run("animal", "ANIMAL", ctx)
    lin = out.lineage
    assert lin is not None and lin.original is not None and lin.original.mint == "orig"
    # since rules 0.31.0 a 4-day-old namesake with no live wave makes it a reference to that
    # name, still inheriting from it
    assert lin.original.match == ["name", "ticker"] and lin.kind == "reference"
    assert lin.same_ticker == 0
    entry = next(c for c in out.copy_of if c.get("recent"))
    assert entry["mint"] == "orig" and entry["match"] == ["name", "ticker"]
    assert {"copycat", "earlier_same_name"} <= _codes(out)
    assert "ticker_collision" not in _codes(out)


# ----------------------------------------------------------------- rank vs "original"


def _wave(name: str, sym: str, before: int, after: int, **kw: Any) -> DbContext:
    """`before` coins launched 1-4 min before this one (siblings, not sources), `after` later."""
    same = [
        _earlier(f"b{i}", name, sym, (60 + 180 * i / max(1, before)) / 3600) for i in range(before)
    ]
    same += [_earlier(f"a{i}", name, sym, -(i + 1) / 60) for i in range(after)]
    return DbContext(same_name=same, **kw)


def test_the_13th_of_15_same_ticker_coins_is_not_an_original() -> None:
    # "Anti-AI Tarpit" $NEPENTHES: 12 $NEPENTHES coins in the minutes before it, 2 after
    out = _run("Anti-AI Tarpit", "NEPENTHES", _wave("Nepenthes", "NEPENTHES", 12, 2))
    lin = out.lineage
    assert lin is not None and lin.kind == "late_copy" and lin.original is None
    assert (lin.rank, lin.rank_of, lin.window_hours) == (13, 15, 24)
    assert "late_copy" in _codes(out)


def test_the_35th_of_38_namesakes_is_not_an_original() -> None:
    # "Markodex The Trencher": 34 Markodex coins launched in the 5 min before it
    out = _run("Markodex The Trencher", "MARKODEX", _wave("Markodex", "MARKODEX", 34, 3))
    lin = out.lineage
    assert lin is not None and lin.kind == "late_copy" and lin.original is None
    assert (lin.rank, lin.rank_of) == (35, 38)
    # "Yuke The Giraffe", the 9th of 9 within 2 min of the post: a copy, not the original
    yuke = _run("Yuke The Giraffe", "YUKE", _wave("Yuke", "YUKE", 8, 0)).lineage
    assert yuke is not None and yuke.kind == "copy" and (yuke.rank, yuke.rank_of) == (9, 9)


def test_the_first_of_a_wave_is_still_the_original() -> None:
    out = _run("Yuke The Giraffe", "YUKE", _wave("Yuke", "YUKE", 0, 8))
    lin = out.lineage
    assert lin is not None and lin.kind == "original" and lin.rank is None


def test_ticker_collisions_do_not_crowd_the_original_out_of_the_prior_reads() -> None:
    from tokensage.engine.lineage import candidate_mints

    n = normalize("Blorbo", "SI", None)
    same = [_earlier(f"c{i}", f"Collision {i}", "SI", 200 - i) for i in range(8)]
    same.append(_earlier("orig", "Blorbo", "SI", 50))
    mints = candidate_mints("mine", NOW, same, [], 30, agrees=lambda name: name_agrees(n, name, K))
    assert mints == ["orig"]
    # without the name check the earliest five namesakes fill the slots
    assert "orig" not in candidate_mints("mine", NOW, same, [], 30)
