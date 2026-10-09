"""Findings from the 2026-10-09 sanity pass over real mints: an initials ticker read as a
dictionary word ($COD, a fish, for "Call Of Duty"), slang read out of a dictionary word
("fren" inside "french"), and the PumpSwap pool of a graduated coin."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tokensage.engine.pipeline import EngineInput, run_basic
from tokensage.resolve import resolver
from tokensage.resolve.pump_ca import PUMP_AMM_PROGRAM, canonical_pool_pda

WHEN = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def _run(name: str, symbol: str, description: str | None = None):  # type: ignore[no-untyped-def]
    return run_basic(EngineInput("M", name, symbol, description, None, WHEN))


def test_call_of_duty_is_the_game_and_its_initials_are_not_a_fish() -> None:
    out = _run("Call Of Duty", "COD")
    cats = dict(out.agg.categories)
    assert "pop_culture" in cats and "animal" not in cats, cats
    assert out.agg.referent is not None and out.agg.referent.label == "Call of Duty"


def test_a_ticker_that_is_not_the_initials_keeps_its_dictionary_sense() -> None:
    assert "animal/fish" in dict(_run("Cod Fish", "COD").agg.categories)


def test_slang_is_not_read_out_of_a_dictionary_word_with_a_two_letter_rest() -> None:
    cats = dict(_run("French Cat", "FRENCHCAT").agg.categories)
    assert "crypto_native/slang" not in cats and "animal/cat" in cats, cats


def test_a_mascot_and_an_everyday_word_still_split() -> None:
    assert "animal/frog" in dict(_run("Frogman", "FROGMAN").agg.categories)


def test_canonical_pool_matches_mainnet() -> None:
    # French Cat graduated to this PumpSwap pool (checked on-chain and on DexScreener)
    assert (
        canonical_pool_pda("k3TjSYCXLPMZBajZGNhAG3ccuE7L3PYMdPFifZqpump")
        == "4dFfW8zNeJeKGABEMai9anGytHXSoU9ijWbFGATde5qx"
    )


class _Rpc:
    def __init__(self, accs: dict[str, dict]) -> None:
        self.accs = accs
        self.reads: list[str] = []

    async def get_account_info(self, pubkey: str, encoding: str = "jsonParsed") -> dict | None:
        self.reads.append(pubkey)
        return self.accs.get(pubkey)


@pytest.mark.parametrize("owner", [PUMP_AMM_PROGRAM, None])
async def test_a_coin_quoted_in_another_token_reads_its_own_pool(owner: str | None) -> None:
    mint, quote = "k3TjSYCXLPMZBajZGNhAG3ccuE7L3PYMdPFifZqpump", PUMP_AMM_PROGRAM
    pool = canonical_pool_pda(mint, quote)
    rpc = _Rpc({pool: {"owner": owner}} if owner else {})
    got = await resolver._graduated_pool(rpc, mint, {"quote_mint": quote}, "SOLPOOL", None)  # type: ignore[arg-type]
    assert rpc.reads == [pool] and got == (pool if owner else None)
