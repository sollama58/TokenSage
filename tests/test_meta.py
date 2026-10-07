"""The current-meta signal (engine/meta.py): copycat rank, same-name meta, hot words."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import httpx
import pytest

from tests.conftest import needs_db
from tokensage.engine import meta
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.meta import MetaCounts, MetaWord
from tokensage.engine.normalize import normalize
from tokensage.engine.pipeline import DbContext, EngineInput, SameNameToken, run_basic

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _namesakes(hours_before: list[float], name: str = "Blorbo", sym: str = "BLORBO"):  # type: ignore[no-untyped-def]
    return [
        SameNameToken(f"m{i}", name, sym, NOW - timedelta(hours=h), "db")
        for i, h in enumerate(hours_before)
    ]


def _run(name: str, sym: str, ctx: DbContext):  # type: ignore[no-untyped-def]
    return run_basic(EngineInput("mine", name, sym, None, None, NOW, ctx=ctx))


def test_copy_rank_counts_namesakes_within_the_window_either_side() -> None:
    same = _namesakes([30, 20, 5, 1, -2, -30])  # -2: launched 2 h after this one
    r = meta.copy_rank("mine", NOW, same, 24)
    assert r is not None
    # within 24 h: 20, 5, 1 before and -2 after (30 and -30 are outside)
    assert r.rank == 4 and r.of == 5
    assert r.phrase("$BLORBO") == "4th of 5 $BLORBO coins launched within 24 h of it"
    assert meta.copy_rank("mine", NOW, _namesakes([30]), 24) is None
    assert meta.copy_rank("mine", None, same, 24) is None
    # the same mint from two sources counts once, and this token never counts itself
    dup = [*same, SameNameToken("m2", "Blorbo", "BLORBO", NOW - timedelta(hours=5), "dex")]
    dup.append(SameNameToken("mine", "Blorbo", "BLORBO", NOW, "pumpfun_search"))
    assert meta.copy_rank("mine", NOW, dup, 24) == r


def test_a_name_launched_many_times_today_is_the_current_meta() -> None:
    ctx = DbContext(same_name=_namesakes([0.5 * i for i in range(1, 41)]))
    out = _run("Blorbo", "BLORBO", ctx)
    cats = dict(out.agg.categories)
    assert cats.get("crypto_native/pumpfun_meta", 0) >= 0.6
    # nothing else knows "Blorbo": the meta is the referent
    assert out.agg.referent is not None
    assert out.agg.referent.label == "current pump.fun meta: Blorbo"
    assert out.agg.referent.kind == "meme"
    # the copycat rank rides on the recent same-name copy
    copy = next(c for c in out.copy_of if c.get("recent"))
    assert (copy["rank"], copy["rank_of"], copy["rank_window_hours"]) == (41, 41, 24)
    assert "copycat_rank:41/41" in copy["signals"]
    assert "41st of 41 $BLORBO coins launched within 24 h of it" in out.summary


def test_the_first_of_many_is_a_meta_but_not_a_copy() -> None:
    same = [
        SameNameToken(f"m{i}", "Blorbo", "BLORBO", NOW + timedelta(hours=0.2 * i), "db")
        for i in range(1, 10)
    ]
    out = _run("Blorbo", "BLORBO", DbContext(same_name=same))
    assert "copycat" not in {f.code for f in out.flags}
    assert out.copy_of == []
    assert "1st of 10 $BLORBO coins" in out.summary
    assert "crypto_native/pumpfun_meta" in dict(out.agg.categories)


def test_a_few_namesakes_are_not_a_meta() -> None:
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_namesakes([3, 1])))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    copy = next(c for c in out.copy_of if c.get("recent"))
    assert copy["rank"] == 3 and copy["rank_of"] == 3  # the rank is still reported
    assert out.agg.referent is None


def test_a_resolved_referent_wins_and_the_meta_only_adds_its_category() -> None:
    out = _run(
        "Elon Musk",
        "MUSKY",
        DbContext(same_name=_namesakes([0.1 * i for i in range(1, 20)], "Elon Musk", "MUSKY")),
    )
    assert out.agg.referent is not None
    assert "meta" not in out.agg.referent.label
    assert "crypto_native/pumpfun_meta" in dict(out.agg.categories)


def test_a_famous_coin_is_not_its_own_meta() -> None:
    out = _run(
        "dogwifhat",
        "WIF",
        DbContext(same_name=_namesakes([0.1 * i for i in range(1, 20)], "dogwifhat", "WIF")),
    )
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)


def test_a_word_spiking_today_is_the_current_meta() -> None:
    k = load_knowledge()
    n = normalize("Zibzab Zibzab Blorpo", "BLORPO", None)
    assert meta.candidate_words(n, k) == ["zibzab", "blorpo"]
    counts = MetaCounts(
        recent_total=1000,
        history_total=60000,
        words=[MetaWord("zibzab", 40, 45), MetaWord("blorpo", 300, 310)],
    )
    out = _run("Zibzab Zibzab Blorpo", "BLORPO", DbContext(meta_counts=counts))
    assert out.agg.referent is not None
    assert out.agg.referent.label == "current pump.fun meta: Blorpo"  # the higher lift
    assert dict(out.agg.categories).get("crypto_native/pumpfun_meta", 0) >= 0.5
    assert "'Blorpo' is a current meta (300 coins in 24 h)" in out.summary


def test_an_always_common_word_is_not_a_meta() -> None:
    # "trump" is in 5% of names today and 5% of names always: no lift
    counts = MetaCounts(1000, 60000, [MetaWord("zorpo", 50, 3000)])
    out = _run("Zorpo Cat", "ZORPO", DbContext(meta_counts=counts))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    # too few launches is not a meta either, however rare the word
    counts = MetaCounts(1000, 60000, [MetaWord("zorpo", 3, 3)])
    assert meta.hot_word(counts, load_knowledge()) is None
    # no history yet: no baseline, no lift
    counts = MetaCounts(1000, 1000, [MetaWord("zorpo", 50, 50)])
    assert meta.hot_word(counts, load_knowledge()) is None


def test_candidate_words_skip_stop_words_digits_and_non_ascii() -> None:
    k = load_knowledge()
    n = normalize("The Official Pepe Coin 2025 中国", "PEPE", None)
    words = meta.candidate_words(n, k)
    assert "pepe" in words
    assert not {"the", "official", "coin", "2025"} & set(words)
    assert all(w.isascii() and w.isalnum() for w in words)
    k2 = replace(k, meta={**k.meta, "max_words": 1})
    assert len(meta.candidate_words(normalize("Alpha Bravo Charlie", "ABC", None), k2)) == 1


# ----------------------------------------------------------------- database


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    try:
        yield conn
    finally:
        await conn.close()


async def _token(conn: asyncpg.Connection, mint: str, name: str, sym: str, at: datetime) -> None:
    await conn.execute(
        "insert into token (mint, name, symbol, created_at) values ($1, $2, $3, $4)",
        mint,
        name,
        sym,
        at,
    )


@needs_db
async def test_db_context_counts_namesakes_and_words_around_the_launch(
    db: asyncpg.Connection,
    settings,  # type: ignore[no-untyped-def]
) -> None:
    from tokensage import analyzer

    # 60 old namesakes (the first query keeps the oldest 50) and 7 within the window
    for i in range(60):
        await _token(db, f"old{i}", "Sahur Cat", "SAHUR", NOW - timedelta(days=10, minutes=i))
    for i in range(7):
        await _token(db, f"new{i}", "Sahur Cat", "SAHUR", NOW - timedelta(hours=i + 1))
    await _token(db, "later", "sahur cat", "SAHUR", NOW + timedelta(hours=3))
    await _token(db, "word1", "Tung Sahur", "TUNG", NOW - timedelta(hours=2))
    await _token(db, "word2", "Sahurday", "SDAY", NOW - timedelta(hours=2))  # not the word
    await _token(db, "mine", "Sahur Cat", "SAHUR", NOW)

    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        ctx = SimpleNamespace(settings=settings, http=http)
        r = SimpleNamespace(mint="mine", created_at=NOW, creator=None)
        dbc = await analyzer._db_context(
            db,
            ctx,
            r,
            None,
            None,
            "SAHUR",
            "sahurcat",
            ["sahur", "cat"],  # type: ignore[arg-type]
        )
    rank = meta.copy_rank("mine", NOW, dbc.same_name, 24)
    assert rank is not None and (rank.rank, rank.of) == (8, 9)
    assert len({t.mint for t in dbc.same_name}) == len(dbc.same_name)  # no duplicates
    assert dbc.meta_counts is not None
    words = {w.word: w for w in dbc.meta_counts.words}
    assert (words["sahur"].recent, words["sahur"].total) == (9, 69)
    assert dbc.meta_counts.recent_total == 10 and dbc.meta_counts.history_total == 70
