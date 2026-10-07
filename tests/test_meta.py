"""The current-meta signal (engine/meta.py): copycat rank, same-name meta, hot words."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
import respx

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
    # a name launched 40 times is a copy relation, not a crypto in-joke theme, and it is no
    # referent: a copy is about what its original is about (nothing known here)
    assert "crypto_native/pumpfun_meta" not in cats and "crypto_native" not in cats
    assert "derivative/copycat" in cats
    assert out.agg.referent is None
    meta_ev = [e for e in out.evidence if e.label == meta.CORPUS_LABEL]
    assert meta_ev and meta_ev[0].source == "meta:name:blorbo"
    assert out.lineage is not None and out.lineage.kind == "late_copy"
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
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    assert out.lineage is not None and out.lineage.kind == "original"


def test_a_few_namesakes_are_not_a_meta() -> None:
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_namesakes([3, 1])))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    copy = next(c for c in out.copy_of if c.get("recent"))
    assert copy["rank"] == 3 and copy["rank_of"] == 3  # the rank is still reported
    assert out.agg.referent is None


def test_a_resolved_referent_wins_and_the_meta_adds_no_category() -> None:
    out = _run(
        "Elon Musk",
        "MUSKY",
        DbContext(same_name=_namesakes([0.1 * i for i in range(1, 20)], "Elon Musk", "MUSKY")),
    )
    assert out.agg.referent is not None
    assert "meta" not in out.agg.referent.label
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    assert "celebrity/elon" in dict(out.agg.categories)  # the theme of its own name


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
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    assert any(e.label == meta.CORPUS_LABEL for e in out.evidence)
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


# ----------------------------------------------------------------- the day's top tokens by volume


def _top(*rows: tuple[str, str, str]) -> list[meta.TopVolume]:
    return [
        meta.TopVolume(i, mint, name, sym, 30e6 - i * 1e6)
        for i, (mint, name, sym) in enumerate(rows, 1)
    ]


TOP = _top(
    ("phub1", "phubber", "PHUBBER"), ("munk1", "winmunk", "MUNK"), ("chonk1", "Le Chonk", "CHONK")
)


def test_sharing_a_name_with_a_top_token_rides_the_days_meta() -> None:
    out = _run("Le Chonk", "LCHONK", DbContext(top_volume=TOP))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    # sharing the name of a top coin is a copy relation: evidence, not a referent
    assert out.agg.referent is None
    ev = next(e for e in out.evidence if e.source == "meta:top:le chonk")
    assert "#3 of the top 3" in ev.detail
    assert "shares its name with $CHONK, #3 by trading volume today" in out.summary
    # the ticker alone is enough
    out = _run("Munk Of The Day", "MUNK", DbContext(top_volume=TOP))
    assert "shares its ticker with $MUNK" in out.summary


def test_a_name_word_shared_with_a_top_token_is_weaker() -> None:
    out = _run("Chonk Cat Zorbo", "CHZ", DbContext(top_volume=TOP))
    cats = dict(out.agg.categories)
    assert "crypto_native/pumpfun_meta" not in cats
    assert out.agg.referent is not None and out.agg.referent.score < 0.45
    assert "'Chonk' is in $CHONK, #3 by trading volume today" in out.summary


def test_a_top_token_itself_is_noted_but_not_its_own_referent() -> None:
    out = run_basic(
        EngineInput("munk1", "winmunk", "MUNK", None, None, NOW, ctx=DbContext(top_volume=TOP))
    )
    assert "#2 by 24 h trading volume on pump.fun" in out.summary
    assert out.agg.referent is None or "meta" not in out.agg.referent.label


def test_no_match_and_short_tickers_add_nothing() -> None:
    top = _top(("up1", "Uncle Pussy", "UP"))
    out = _run("Up Only", "UP", DbContext(top_volume=top))  # a 2-letter ticker is too generic
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    out = _run("Zorbo", "ZORBO", DbContext(top_volume=TOP))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)
    # an everyday word shared with a top token is not riding it
    top = _top(("kc1", "Knight Cat", "KCAT"))
    out = _run("Lucky Cat", "LCAT", DbContext(top_volume=top))
    assert "crypto_native/pumpfun_meta" not in dict(out.agg.categories)


def test_a_launch_count_meta_and_the_top_match_both_add_context() -> None:
    ctx = DbContext(
        same_name=_namesakes([0.5 * i for i in range(1, 10)], "Le Chonk", "LCHONK"), top_volume=TOP
    )
    out = _run("Le Chonk", "LCHONK", ctx)
    assert out.agg.referent is None
    assert sum(1 for e in out.evidence if e.label == meta.CORPUS_LABEL) == 2
    assert "(a current meta)" in out.summary and "#3 by trading volume today" in out.summary


def _gt_page(*pools: tuple[str, str, str, float]) -> dict:  # type: ignore[type-arg]
    return {
        "data": [
            {
                "type": "pool",
                "attributes": {"volume_usd": {"h24": str(vol)}},
                "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}},
            }
            for mint, _, _, vol in pools
        ],
        "included": [
            {
                "id": f"solana_{mint}",
                "type": "token",
                "attributes": {"address": mint, "name": n, "symbol": s},
            }
            for mint, n, s, _ in pools
        ],
    }


@respx.mock
async def test_geckoterminal_top_tokens_merge_pools_and_skip_majors() -> None:
    from tokensage.sources import geckoterminal

    respx.get(url__regex=r".*/dexes/pumpswap/pools.*").mock(
        return_value=httpx.Response(
            200,
            json=_gt_page(
                ("A", "alpha", "ALPHA", 10e6),
                ("So11111111111111111111111111111111111111112", "Wrapped SOL", "SOL", 99e6),
                ("B", "beta", "BETA", 8e6),
            ),
        )
    )
    respx.get(url__regex=r".*/dexes/pump-fun/pools.*").mock(
        side_effect=[
            httpx.Response(
                200, json=_gt_page(("B", "beta", "BETA", 5e6), ("C", "gamma", "GAMMA", 1e6))
            ),
            httpx.Response(429),
        ]
    )
    async with httpx.AsyncClient() as http:
        top = await geckoterminal.top_tokens(http, ["pumpswap", "pump-fun"], 1, 2, pause_s=0)
        assert top is not None
        assert [(t.mint, t.volume_usd, t.dex) for t in top] == [
            ("B", 13e6, "pumpswap"),
            ("A", 10e6, "pumpswap"),
        ]
        # every page failing: None, so yesterday's list stays
        assert await geckoterminal.top_tokens(http, ["pump-fun"], 1, 25, pause_s=0) is None


@needs_db
async def test_refresh_top_volume_stores_todays_snapshot_and_the_analyzer_reads_it(
    db: asyncpg.Connection,
    settings,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokensage import analyzer
    from tokensage.jobs import knowledge as kjob
    from tokensage.sources import geckoterminal

    async def fake_top(*_a, **_k):  # type: ignore[no-untyped-def]
        return [
            geckoterminal.TopToken("chonk1", "Le Chonk", "CHONK", 25e6, "pumpswap"),
            geckoterminal.TopToken("munk1", "winmunk", "MUNK", 20e6, "pump-fun"),
        ]

    monkeypatch.setattr(geckoterminal, "top_tokens", fake_top)
    await db.execute(
        "insert into top_volume (day, rank, mint) values (current_date - 400, 1, 'ancient')"
    )
    async with httpx.AsyncClient() as http:
        res = await kjob.refresh_top_volume(db, http, pause_s=0)
        assert res == {"stored": 2, "pruned": 1}
        assert (await kjob.refresh_top_volume(db, http, pause_s=0))[
            "stored"
        ] == 2  # rerun: no dupes
    assert await db.fetchval("select count(*) from top_volume") == 2

    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        ctx = SimpleNamespace(settings=settings, http=http)
        r = SimpleNamespace(mint="mine", created_at=datetime.now(UTC), creator=None)
        dbc = await analyzer._db_context(db, ctx, r, None, None, "", "", [])  # type: ignore[arg-type]
        assert [(t.rank, t.symbol) for t in dbc.top_volume] == [(1, "CHONK"), (2, "MUNK")]
        # a token launched long before any snapshot gets none
        r = SimpleNamespace(mint="mine", created_at=NOW - timedelta(days=30), creator=None)
        dbc = await analyzer._db_context(db, ctx, r, None, None, "", "", [])  # type: ignore[arg-type]
        assert dbc.top_volume == []
