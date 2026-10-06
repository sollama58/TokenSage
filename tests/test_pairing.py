"""The token a coin trades against (bonding-curve quote mint) feeds its meaning."""

from __future__ import annotations

import asyncpg
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import CID_META, T22_MINT, USDC, FakeChain, install_web, metadata_json
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage.api.schemas import TokenResponse
from tokensage.engine.pairing import PairInput, neutral
from tokensage.engine.pipeline import EngineInput, run_basic
from tokensage.taxonomy import category_labels, flag_codes

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


def _run(name: str, symbol: str, pair: PairInput | None):  # type: ignore[no-untyped-def]
    return run_basic(
        EngineInput(
            mint=T22_MINT,
            name=name,
            symbol=symbol,
            description=None,
            image_bytes=None,
            created_at=None,
            pair=pair,
        )
    )


def _bonk() -> PairInput:
    return PairInput(mint=BONK, symbol="BONK", name="Bonk", source="onchain")


def test_taxonomy_has_the_pair_labels() -> None:
    labels = category_labels()
    assert {"crypto_native/paired_ecosystem", "derivative/pair_family"} <= labels
    assert "non_sol_pair" in flag_codes()


def test_name_building_on_the_pair_token_takes_its_referent() -> None:
    out = _run("Baby Bonk", "BBONK", _bonk())
    assert out.pair is not None and out.pair.builds_on
    assert out.agg.referent is not None and out.agg.referent.label == "Bonk"
    cats = dict(out.agg.categories)
    assert cats["derivative/pair_family"] >= 0.7
    assert cats["crypto_native/paired_ecosystem"] >= 0.5
    flag = next(f for f in out.flags if f.code == "non_sol_pair")
    assert "$BONK" in flag.detail and "builds on" in flag.detail


def test_unrelated_name_keeps_its_own_meaning() -> None:
    out = _run("Cat In Hat", "CIH", _bonk())
    assert out.pair is not None and not out.pair.builds_on
    assert out.pair.referent is not None and out.pair.referent.label == "Bonk"
    # the pair is context: the coin is still read as a cat coin, not as Bonk
    assert out.agg.referent is None or out.agg.referent.label != "Bonk"
    cats = dict(out.agg.categories)
    assert cats.get("animal/cat", 0) > cats.get("animal/dog", 0)
    assert "crypto_native/paired_ecosystem" in cats
    assert "derivative/pair_family" not in cats
    assert any(f.code == "non_sol_pair" for f in out.flags)


def test_paired_ecosystem_does_not_lift_crypto_native() -> None:
    plain = _run("Moon Frog", "MFROG", None)
    paired = _run("Moon Frog", "MFROG", PairInput(mint="Q" * 32, symbol="ZZQX", name="Zzqx"))
    assert dict(paired.agg.categories).get("crypto_native", 0) == dict(plain.agg.categories).get(
        "crypto_native", 0
    )


@pytest.mark.parametrize("quote", ["SOL", USDC])
def test_sol_and_stablecoins_carry_no_meaning(quote: str) -> None:
    pair = neutral(quote)
    assert pair is not None and pair.kind in ("sol", "stablecoin")
    plain = _run("Baby Bonk", "BBONK", None)
    out = _run("Baby Bonk", "BBONK", pair)
    assert out.pair is not None and not out.pair.evidence
    assert out.agg.categories == plain.agg.categories
    assert not any(f.code == "non_sol_pair" for f in out.flags)


def test_stored_analysis_referent_is_used() -> None:
    from tokensage.engine.context import ReferentCandidate

    pair = PairInput(
        mint="Q" * 32,
        symbol="QWZX",
        name="Qwzx",
        source="analysis",
        referent=ReferentCandidate("Moo Deng", "famous_animal", "baby pygmy hippo", "x", 0.9),
        categories=[("animal/hippo", 0.9), ("animal", 0.9)],
    )
    out = _run("Qwzx Jr", "QWZXJR", pair)
    assert out.pair is not None and out.pair.builds_on
    assert out.agg.referent is not None and out.agg.referent.label == "Moo Deng"
    assert "animal/hippo" in dict(out.agg.categories)


# ----------------------------------------------------------------- end to end


@needs_db
async def test_analysis_reports_and_uses_the_pair(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Baby Bonk", "BBONK", META_URI, quote=BONK)
    chain.add_spl_token(BONK, "Bonk", "BONK")
    install_web(router, chain, meta=metadata_json(name="Baby Bonk", symbol="BBONK", twitter=None))
    async with make_client(migrated_db) as c:
        r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 5})
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None
    assert a.market.quote_mint == BONK
    p = a.market.pair
    assert p is not None and p.kind == "token" and p.symbol == "BONK" and p.name == "Bonk"
    assert p.source == "onchain" and p.builds_on
    assert p.referent is not None and p.referent.label == "Bonk"
    assert a.referent is not None and a.referent.label == "Bonk"
    assert any(f.code == "non_sol_pair" for f in a.flags)
    assert any(c.label == "derivative/pair_family" for c in a.categories)
    assert any(e.kind == "pair" and e.source == "pair:BONK" for e in a.evidence)

    conn = await asyncpg.connect(migrated_db)
    try:
        row = await conn.fetchrow("select name, symbol, source from pair_token where mint=$1", BONK)
    finally:
        await conn.close()
    assert row is not None and row["symbol"] == "BONK" and row["source"] == "onchain"


@needs_db
async def test_pair_token_name_is_cached(
    migrated_db: str,
    clean_tables: None,
) -> None:
    from tokensage.resolve import pair as pair_lookup

    class _NoRpc:
        async def get_account_info(self, *a: object, **k: object) -> None:
            raise AssertionError("cached pair token must not hit RPC")

    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            "insert into pair_token (mint, name, symbol, source) "
            "values ($1, 'Bonk', 'BONK', 'onchain')",
            BONK,
        )
        p = await pair_lookup.lookup(conn, _NoRpc(), BONK)  # type: ignore[arg-type]
        assert p is not None and p.symbol == "BONK" and p.source == "onchain"
        s = await pair_lookup.lookup(conn, _NoRpc(), "SOL")  # type: ignore[arg-type]
        assert s is not None and s.kind == "sol"
        assert await pair_lookup.lookup(conn, _NoRpc(), None) is None  # type: ignore[arg-type]
    finally:
        await conn.close()


@needs_db
async def test_sol_pair_is_reported_without_effect(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "dog wif cap", "cap", META_URI)
    install_web(router, chain)
    async with make_client(migrated_db) as c:
        r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 5})
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.market.quote_mint == "SOL"
    assert a.market.pair is not None and a.market.pair.kind == "sol"
    assert not any(f.code == "non_sol_pair" for f in a.flags)


# ----------------------------------------------------------------- tokenized stocks (xStocks)

XS_MINT = "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB"


def _xstock(symbol: str = "TSLAx", name: str = "Tesla xStock", mint: str = XS_MINT) -> PairInput:
    return PairInput(mint=mint, symbol=symbol, name=name, source="onchain")


def test_xstock_detection_needs_the_name_or_the_backed_mint() -> None:
    from tokensage.engine.pairing import xstock_ticker

    assert xstock_ticker("TSLAx", "Tesla xStock", "anything") == "TSLA"
    assert xstock_ticker("NVDAx", "NVIDIA", XS_MINT) == "NVDA"
    assert xstock_ticker("BRK.Bx", "Berkshire Hathaway xStock", "m") == "BRKB"
    assert xstock_ticker("MAX", "Maximus", "m") is None  # shape alone is not enough
    assert xstock_ticker("SEX", "Sex", XS_MINT[:2] + "z" * 41) is None  # uppercase X, no name
    assert xstock_ticker(None, "Tesla xStock", "m") is None


def test_coin_building_on_the_paired_stock_takes_the_company() -> None:
    out = _run("Tesla Moon", "TMOON", _xstock())
    p = out.pair
    assert p is not None and p.kind == "tokenized_stock" and p.underlying == "TSLA" and p.builds_on
    assert out.agg.referent is not None and out.agg.referent.label == "Tesla (TSLA)"
    cats = dict(out.agg.categories)
    assert cats["tradfi/tokenized_stock"] >= 0.5 and cats.get("celebrity/elon", 0) > 0.5
    assert "crypto_native/paired_ecosystem" not in cats  # the stock label says it instead
    flag = next(f for f in out.flags if f.code == "non_sol_pair")
    assert "tokenized $TSLA stock" in flag.detail
    assert "Context: trades against $TSLAx" in out.summary


def test_ticker_building_on_the_stock_counts_too() -> None:
    out = _run("Green Candles", "NVDAMOON", _xstock("NVDAx", "NVIDIA xStock"))
    assert out.pair is not None and out.pair.builds_on
    assert "contains the stock ticker $NVDA" in (out.pair.builds_on_detail or "")


def test_unrelated_coin_on_a_stock_pair_keeps_its_own_meaning() -> None:
    out = _run("Robotaxi", "TAXI", _xstock())
    assert out.pair is not None and not out.pair.builds_on
    assert out.agg.referent is None or "Tesla" not in out.agg.referent.label
    assert "tradfi/tokenized_stock" in dict(out.agg.categories)


def test_unknown_xstock_still_reads_as_a_stock() -> None:
    out = _run("Moon", "MOON", _xstock("ZZZQx", "Zed Corp xStock"))
    assert out.pair is not None and out.pair.kind == "tokenized_stock"
    assert out.pair.underlying == "ZZZQ" and out.pair.referent is None


@pytest.mark.parametrize(
    "mint,kind",
    [
        ("J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "lst"),
        ("cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "major"),
        ("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "stablecoin"),
    ],
)
def test_staked_sol_wrapped_majors_and_more_stables_are_neutral(mint: str, kind: str) -> None:
    pair = neutral(mint)
    assert pair is not None and pair.kind == kind
    out = _run("Baby Bonk", "BBONK", pair)
    assert out.pair is not None and not out.pair.evidence
    assert not any(f.code == "non_sol_pair" for f in out.flags)


def test_stock_names_and_tickers_are_understood_in_the_coin_itself() -> None:
    for name, sym in (("Tesla Robot", "TROBOT"), ("Nvidia Gang", "NVDAG"), ("TSLAx Moon", "TXM")):
        out = _run(name, sym, None)
        assert any(e.source.startswith("entities:") and "(" in e.source for e in out.evidence), name
    # common words that are also company names or tickers do not turn into stocks
    for name, sym in (("Apple Pie", "PIE"), ("Coin Hood", "HOOD"), ("Max", "MAX")):
        out = _run(name, sym, None)
        assert "tradfi/stock" not in dict(out.agg.categories), name


def test_short_names_are_not_read_from_compound_fragments() -> None:
    out = _run("Robotaxi", "TAXI", None)  # segments as "robot a xi"; "xi" is not Xi Jinping
    assert out.agg.referent is None or "Xi" not in out.agg.referent.label


@needs_db
async def test_unknown_pump_pair_token_is_queued_for_analysis(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    pump_pair = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM".replace("WWM", "ump")
    pump_pair = pump_pair[:-4] + "pump"
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Moon Frog", "MFROG", META_URI, quote=pump_pair)
    chain.add_spl_token(pump_pair, "Zzqx", "ZZQX")
    install_web(router, chain, meta=metadata_json(name="Moon Frog", symbol="MFROG", twitter=None))
    async with make_client(migrated_db) as c:
        r = await c.get(f"/v1/tokens/{T22_MINT}", params={"depth": "basic", "wait": 5})
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.referent is None or a.referent.supported_by is not None
    conn = await asyncpg.connect(migrated_db)
    try:
        queued = await conn.fetchval(
            "select count(*) from job where kind='analyze' and mint=$1 and depth='basic'",
            pump_pair,
        )
    finally:
        await conn.close()
    assert queued == 1
