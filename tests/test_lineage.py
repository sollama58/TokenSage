"""Lineage (engine/lineage.py), copy inheritance (pipeline._inherit) and the waves."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import httpx
import pytest

from tests.conftest import needs_db
from tokensage.engine import image as image_stage
from tokensage.engine.context import ReferentCandidate
from tokensage.engine.pipeline import (
    DbContext,
    EngineInput,
    PriorRead,
    SameNameToken,
    run_basic,
)

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
LOGO = 0x0F0F_0F0F_0F0F_0F0F
MIRROR = 0x7777_0000_7777_0000


def _feats(phash: int = LOGO, mirror: int = MIRROR) -> image_stage.ImageFeatures:
    return image_stage.ImageFeatures(phash, 0, mirror, 64, 64, False, 1, [], [], "PNG")


def _logo(mint: str, hours: float, phash: int = LOGO, name: str = "Zed", sym: str = "ZED"):  # type: ignore[no-untyped-def]
    return image_stage.Candidate(
        f"ipfs:{mint}", phash, mint=mint, created_at=NOW - timedelta(hours=hours), name=name,
        symbol=sym,
    )  # fmt: skip


def _run(name: str, sym: str, ctx: DbContext, logo: bool = False, mint: str = "mine"):  # type: ignore[no-untyped-def]
    inp = EngineInput(mint, name, sym, None, None, NOW, ctx=ctx)
    if logo:
        inp.logo_features = _feats()
    return run_basic(inp)


def _names(hours: list[float], name: str = "Blorbo", sym: str = "BLORBO") -> list[SameNameToken]:
    return [
        SameNameToken(f"m{i}", name, sym, NOW - timedelta(hours=h), "db")
        for i, h in enumerate(hours)
    ]


def test_an_original_counts_its_siblings_but_copies_nothing() -> None:
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_names([-1, -2])))  # later launches
    assert out.lineage is not None and out.lineage.kind == "original"
    assert out.lineage.original is None
    assert (out.lineage.siblings_1h, out.lineage.siblings_24h) == (1, 1)  # no look-ahead


def test_the_rank_and_the_age_of_the_original_set_the_kind() -> None:
    early = _run("Blorbo", "BLORBO", DbContext(same_name=_names([1, 0.5])))
    assert early.lineage is not None and early.lineage.kind == "early_copy"
    assert early.lineage.original is not None and early.lineage.original.mint == "m0"
    assert (early.lineage.rank, early.lineage.rank_of) == (3, 3)
    assert early.lineage.siblings_1h == 3 and early.lineage.siblings_6h == 3
    mid = _run("Blorbo", "BLORBO", DbContext(same_name=_names([12])))
    assert mid.lineage is not None and mid.lineage.kind == "copy"
    late = _run("Blorbo", "BLORBO", DbContext(same_name=_names([0.1 * i for i in range(1, 15)])))
    assert late.lineage is not None and late.lineage.kind == "late_copy"
    assert late.lineage.rank == 15
    assert "late_copy" in {f.code for f in late.flags}
    old = _run("Blorbo", "BLORBO", DbContext(same_name=_names([48])))
    assert old.lineage is not None and old.lineage.kind == "late_copy"
    assert "late_copy" in {f.code for f in old.flags}


def test_a_logo_copy_gets_its_own_copy_of_entry_and_counts() -> None:
    cands = [_logo("a", 3), _logo("b", 2, LOGO ^ 0b111), _logo("c", 1, MIRROR), _logo("d", 30)]
    out = _run("Totally New", "NEWW", DbContext(image_candidates=cands), logo=True)
    lin = out.lineage
    assert lin is not None and lin.kind == "late_copy"  # "d" launched 30 h earlier
    assert lin.original is not None and lin.original.mint == "d"
    assert lin.original.match == ["image"] and lin.original.image_distance == 0
    assert lin.logo_reuse_24h == 3 and lin.siblings_24h == 4
    assert lin.logo_first_seen_at == NOW - timedelta(hours=30)
    entry = next(c for c in out.copy_of if c["mint"] == "d")
    assert entry["recent"] is True and entry["match"] == ["image"]
    assert entry["original_age_s"] == 30 * 3600
    assert "logo_reused" in {f.code for f in out.flags}
    # a fresh logo was first seen with this coin
    fresh = _run("Totally New", "NEWW", DbContext(), logo=True)
    assert fresh.lineage is not None and fresh.lineage.logo_first_seen_at == NOW
    assert fresh.lineage.logo_reuse_24h == 0


def test_a_coin_sharing_name_and_logo_is_the_original_over_an_earlier_namesake() -> None:
    same = _names([5, 2])  # m0 5 h earlier, m1 2 h earlier
    cands = [_logo("m1", 2, name="Blorbo", sym="BLORBO")]
    out = _run("Blorbo", "BLORBO", DbContext(same_name=same, image_candidates=cands), logo=True)
    lin = out.lineage
    assert lin is not None and lin.original is not None and lin.original.mint == "m1"
    assert lin.original.match == ["name", "ticker", "image"]
    named = next(c for c in out.copy_of if c["mint"] == "m0")  # the copycat entry as before
    assert named["match"] == ["name", "ticker"] and named["original_age_s"] == 5 * 3600


def test_a_clone_of_a_famous_coin_is_a_reference_not_the_coin() -> None:
    from tokensage.engine.knowledge import load_knowledge

    bonk = next(c for c in load_knowledge().coins if c.symbol == "BONK")
    real = replace(bonk, mint="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", source="db")
    clone = _run("Bonk", "BONK", DbContext(extra_coins=[real]))
    assert clone.lineage is not None and clone.lineage.kind == "reference"
    assert clone.lineage.reference is not None and clone.lineage.reference.mint == real.mint
    itself = _run("Bonk", "BONK", DbContext(extra_coins=[real]), mint=real.mint or "")
    assert itself.lineage is not None and itself.lineage.kind == "original"


def test_a_copy_inherits_the_referent_with_its_own_support() -> None:
    prior = PriorRead(
        categories=[("animal", 0.9), ("animal/frog", 0.9), ("derivative", 0.5)],
        referent=ReferentCandidate("Blorbo the Frog", "meme", None, "analysis", 0.8),
    )
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_names([3]), originals={"m0": prior}))
    cats = dict(out.agg.categories)
    assert cats["animal/frog"] == pytest.approx(0.72)
    assert cats["animal"] == pytest.approx(0.72)  # lifted once, by the child
    assert out.agg.referent is not None and out.agg.referent.label == "Blorbo the Frog"
    assert out.agg.referent.score == pytest.approx(0.64)
    # a copy with a theme of its own keeps it and takes no categories from the original
    own = _run("Blorbo Cat", "BLORBO", DbContext(same_name=_names([3]), originals={"m0": prior}))
    assert "animal/frog" not in dict(own.agg.categories)
    assert "animal/cat" in dict(own.agg.categories)


def test_a_placeholder_referent_is_never_inherited() -> None:
    prior = PriorRead(
        referent=ReferentCandidate("current pump.fun meta: Blorbo", "meme", None, "meta", 0.5)
    )
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_names([3]), originals={"m0": prior}))
    assert out.agg.referent is None


# ----------------------------------------------------------------- database


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def _token(
    conn: asyncpg.Connection, mint: str, at: datetime, key: str | None = None, phash: int = LOGO
) -> None:
    await conn.execute(
        "insert into token (mint, name, symbol, created_at) values ($1, 'Zed', 'ZED', $2)",
        mint,
        at,
    )
    if key:
        await conn.execute(
            "insert into image (content_key, phash) values ($1, $2) on conflict do nothing",
            key,
            phash - (1 << 64) if phash >= 1 << 63 else phash,
        )
        await conn.execute(
            """insert into token_metadata (mint, status, image_content_key)
               values ($1, 'ok', $2)""",
            mint,
            key,
        )


@needs_db
async def test_db_context_finds_logo_near_duplicates_and_prior_reads(
    db: asyncpg.Connection,
    settings,  # type: ignore[no-untyped-def]
) -> None:
    from tokensage import analyzer

    await _token(db, "same_file", NOW - timedelta(hours=2), "ipfs:shared")  # identical file
    await _token(db, "edited", NOW - timedelta(hours=3), "ipfs:edited", LOGO ^ 0b1011)
    await _token(db, "mirrored", NOW - timedelta(hours=4), "ipfs:mirror", MIRROR)
    await _token(db, "other", NOW - timedelta(hours=1), "ipfs:other", ~LOGO & ((1 << 64) - 1))
    await _token(db, "too_old", NOW - timedelta(days=9), "ipfs:old")
    await _token(db, "later", NOW + timedelta(hours=1), "ipfs:later")
    await _token(db, "mine", NOW, "ipfs:shared")
    await db.execute(
        """insert into analysis (mint, version, depth, doc) values ('edited', 1, 'basic', $1)""",
        {
            "categories": [{"label": "animal/frog", "confidence": 0.8}],
            "referent": {"label": "Zed Frog", "kind": "meme", "confidence": 0.7},
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as http:
        ctx = SimpleNamespace(settings=settings, http=http)
        r = SimpleNamespace(mint="mine", created_at=NOW, creator=None)
        dbc = await analyzer._db_context(
            db,
            ctx,
            r,
            None,
            None,
            "ZED",
            "zed",
            [],
            logo=_feats(),  # type: ignore[arg-type]
        )
    found = {c.mint for c in dbc.image_candidates if c.mint}
    assert found == {"same_file", "edited", "mirrored"}
    assert "edited" in dbc.originals
    prior = dbc.originals["edited"]
    assert prior.categories == [("animal/frog", 0.8)]
    assert prior.referent is not None and prior.referent.label == "Zed Frog"


@needs_db
async def test_referent_and_category_waves_count_recent_reads(
    db: asyncpg.Connection,
) -> None:
    from tokensage import analyzer
    from tokensage.api.schemas import Analysis, Category, Referent, Versions

    now = datetime.now(UTC)

    async def read(mint: str, at: datetime, label: str | None, cats: list[str]) -> None:
        await db.execute("insert into token (mint, created_at) values ($1, $2)", mint, at)
        doc = Analysis(
            mint=mint,
            created_at=at,
            referent=Referent(label=label, kind="person", confidence=0.9) if label else None,
            categories=[Category(label=c, confidence=0.8) for c in cats],
            summary="x",
            depth="basic",
            analyzed_at=now,
            versions=Versions(rules="t", lexicon="t"),
        )
        await analyzer._store_analysis(db, doc)

    await read("a", now - timedelta(minutes=30), "Elon Musk", ["celebrity/elon"])
    await read("b", now - timedelta(hours=3), "Elon Musk", ["celebrity"])
    await read("c", now - timedelta(hours=20), "elon  musk", ["celebrity/elon"])
    await read("d", now - timedelta(days=3), "Elon Musk", [])
    await read("e", now - timedelta(minutes=5), "Donald Trump", ["celebrity/elon"])
    await db.execute("insert into token (mint, created_at) values ('mine', $1)", now)

    agg = SimpleNamespace(
        referent=SimpleNamespace(label="Elon Musk"), categories=[("celebrity/elon", 0.8)]
    )
    out = SimpleNamespace(copy_of=[], lineage=None, agg=agg)
    ctx = SimpleNamespace(rpc=None)
    r = SimpleNamespace(mint="mine", created_at=now - timedelta(seconds=1))
    ex = await analyzer._read_extras(db, ctx, r, out)  # type: ignore[arg-type]
    assert ex.wave is not None
    assert (ex.wave.launches_1h, ex.wave.launches_6h, ex.wave.launches_24h) == (2, 3, 4)
    assert ex.wave.rank_24h == 4
    assert ex.wave.first_seen_at == now - timedelta(days=3)
    assert ex.category_waves == {"celebrity/elon": 3}  # a, e and this one


def test_a_namesake_from_two_minutes_earlier_is_a_sibling_not_the_original() -> None:
    out = _run("Blorbo", "BLORBO", DbContext(same_name=_names([2 / 60])))
    assert out.lineage is not None and out.lineage.kind == "original"
    assert out.lineage.siblings_1h == 2


def test_a_copy_of_a_top_volume_coin_names_it_as_the_original() -> None:
    from tokensage.engine.meta import TopVolume

    top = [TopVolume(1, "topmint", "Le Chonk", "CHONK", 3e7)]
    out = _run("Le Chonk", "LCHONK", DbContext(top_volume=top))
    assert out.lineage is not None and out.lineage.kind == "copy"
    assert out.lineage.original is not None and out.lineage.original.mint == "topmint"
    assert out.lineage.original.match == ["name"]


def test_an_inherited_referent_never_inflates_the_copys_own() -> None:
    base = _run("Elon Musk", "ELON", DbContext(same_name=_names([3], "Elon Musk", "ELON")))
    assert base.agg.referent is not None
    prior = PriorRead(referent=replace(base.agg.referent))
    out = _run(
        "Elon Musk",
        "ELON",
        DbContext(same_name=_names([3], "Elon Musk", "ELON"), originals={"m0": prior}),
    )
    assert out.agg.referent is not None
    assert out.agg.referent.score == base.agg.referent.score
