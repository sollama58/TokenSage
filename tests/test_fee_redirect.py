"""Creator-fee redirects (rules 0.19.0): holder rewards, cashback, fee-sharing configs paying
the creator, other wallets, GitHub-linked accounts and donate.gg charities.

The golden cases in tests/golden/fee_cases.yaml replay real mainnet accounts captured on
2026-10-08 (tests/fixtures/fee_accounts.json) through the resolver's fee logic."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx
import yaml

from tests.conftest import needs_db
from tests.fixtures.chain import (
    CID_META,
    CREATOR,
    RPC,
    T22_MINT,
    USDC,
    WALLET,
    FakeChain,
    install_web,
    public_resolver,
    social_fee_pda_bytes,
)
from tokensage.analyzer import build_document
from tokensage.api.schemas import Analysis
from tokensage.config import Settings
from tokensage.engine.pipeline import RULES_VERSION
from tokensage.net import safe_fetch
from tokensage.resolve import fees
from tokensage.resolve.pump_ca import bonding_curve_pda, decode_bonding_curve
from tokensage.resolve.resolver import Resolved, resolve
from tokensage.resolve.rpc import SolanaRpc

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden" / "fee_cases.yaml"
CAPTURED: dict[str, Any] = json.loads((FIXTURES / "fee_accounts.json").read_text())
CASES: list[dict[str, Any]] = yaml.safe_load(GOLDEN.read_text())["cases"]
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


def _b64(case: dict[str, Any], addr: str) -> bytes:
    import base64

    return base64.b64decode(case["accounts"][addr]["data"])


def _curve(case: dict[str, Any]) -> dict[str, Any]:
    return decode_bonding_curve(_b64(case, case["bonding_curve"]))


def _acct(case: dict[str, Any], addr: str) -> dict | None:
    a = case["accounts"].get(addr)
    return (
        None
        if a is None
        else {"owner": a["owner"], "lamports": a["lamports"], "data": [a["data"], "base64"]}
    )


async def _replay(
    case: dict[str, Any], chain: FakeChain | None = None
) -> tuple[fees.CreatorFee, FakeChain]:
    chain = chain or FakeChain()
    chain.load_captured(case)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            router.post(RPC).mock(side_effect=chain.handle)
            rpc = SolanaRpc(RPC, http)
            cf = await fees.resolve_creator_fee(
                case["mint"],
                _curve(case),
                _acct(case, case["sharing_config"]),
                rpc=rpc,
                conn=None,
                http=http,
                lookup_github=False,
            )
    return cf, chain


# ----------------------------------------------------------------- PDAs and decoders


def test_pdas_match_mainnet() -> None:
    for case in CAPTURED.values():
        assert fees.sharing_config_pda(case["mint"]) == case["sharing_config"]
        assert bonding_curve_pda(case["mint"]) == case["bonding_curve"]
    holder = CAPTURED["holder_rewards_shitcoin"]
    assert _curve(holder)["creator"] == fees.holder_rewards_pda(holder["mint"])
    # the GitHub recipient's address derives from ["social-fee-pda", user_id, platform]
    assert fees.social_fee_pda("258455447", 2) == "9VZA4fKUT5SQXiirfdoFaxpA87bHGBUfmjWxhHFhTkpP"
    # the charity escrow derives from ["donation-fee-pda", mint, config_id]
    charity = CAPTURED["charity_qizai"]
    assert (
        fees.donation_fee_pda(charity["mint"], "JCZBhPewgvoAYHCmirP3vvXe3VpXiXVY1gSsj1o2ZsCw")
        == "37Q9WtbVtiBZHw3MT4MvzxDbugR3kEPNYS2SsN1HoW89"
    )


def test_decoders_on_real_accounts() -> None:
    gh = CAPTURED["github_shelldon"]
    sc = fees.decode_sharing_config(_b64(gh, gh["sharing_config"]))
    assert sc["mint"] == gh["mint"] and sc["status"] == "active" and sc["admin_revoked"]
    assert sc["shareholders"] == [
        {"address": "9VZA4fKUT5SQXiirfdoFaxpA87bHGBUfmjWxhHFhTkpP", "share_bps": 10_000}
    ]
    soc = fees.decode_social_fee_pda(_b64(gh, "9VZA4fKUT5SQXiirfdoFaxpA87bHGBUfmjWxhHFhTkpP"))
    assert soc["user_id"] == "258455447" and soc["platform"] == 2
    assert soc["total_claimed"] == 419_919_332_737  # 419.9 SOL claimed by that GitHub user
    stj = CAPTURED["charity_stjude"]
    don = fees.decode_donation_fee_pda(_b64(stj, "CMvJyHndck7H2FiDpBAqhfMdyRtmsASPWUTem3UBitWQ"))
    assert don["base_mint"] == stj["mint"]
    assert don["config_id"] == "H16fEMZN9b8Zmhh5Ara343WdZdSdgn2P8oUkAGdB57Ru"
    assert don["total_donated"] == 1_544_637_985_579
    with pytest.raises(ValueError):
        fees.decode_sharing_config(b"\0" * 100)
    with pytest.raises(ValueError):
        fees.decode_social_fee_pda(fees.SOCIAL_FEE_PDA_DISC + b"\0\0" + b"\xff\xff\xff\xff")


def test_classify_recipient_account() -> None:
    assert fees.classify_recipient_account("x", None) == {"kind": "wallet"}
    assert fees.classify_recipient_account(
        "x", {"owner": fees.SYSTEM_PROGRAM, "data": ["", "base64"]}
    ) == {"kind": "wallet"}
    other = fees.classify_recipient_account(
        "x", {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": ["", "base64"]}
    )
    assert other["kind"] == "program"
    junk = fees.classify_recipient_account(
        "x", {"owner": fees.PUMP_FEES_PROGRAM, "data": ["AAAA", "base64"]}
    )
    assert junk["kind"] == "program"


# ----------------------------------------------------------------- golden cases


@pytest.mark.parametrize("case", CASES, ids=[c["case"] for c in CASES])
async def test_golden_fee_case(case: dict[str, Any]) -> None:
    captured = CAPTURED[case["case"]]
    cf, chain = await _replay(captured)
    assert cf.destination == case["destination"], cf
    assert cf.mechanism == case["mechanism"]
    assert [[r.kind, r.share_bps] for r in cf.recipients] == case["recipients"]
    assert cf.split is case.get("split", len(case["recipients"]) > 1)
    assert chain.calls.count("getMultipleAccounts") == case["extra_rpc_calls"]
    if "github_user_id" in case:
        gh = [r for r in cf.recipients if r.kind == "github"][0]
        assert gh.user_id == case["github_user_id"] and gh.platform == "github"
        assert gh.url == f"https://api.github.com/user/{case['github_user_id']}"
        assert gh.lifetime_received == pytest.approx(419.919332737)
    if "charity_config_id" in case:
        ch = [r for r in cf.recipients if r.kind == "charity"][0]
        assert ch.charity_config_id == case["charity_config_id"]
    assert sum(cf.shares.values()) == pytest.approx(1.0) or not cf.recipients
    # what the API reports, through build_document
    r = _resolved(captured["mint"], cf, _curve(captured))
    doc = build_document(r, None, "basic", None, None)
    Analysis.model_validate(doc.model_dump(mode="json"))
    assert doc.market.creator == case["creator"]
    assert doc.market.creator_kind == case["creator_kind"]
    assert doc.market.creator_onchain == _curve(captured)["creator"]
    assert doc.market.creator_fee is not None
    assert doc.market.creator_fee.destination == case["destination"]
    assert (
        doc.market.creator_fee.summary.startswith("creator fees")
        or case["destination"] == "unknown"
    )
    codes = [f.code for f in doc.flags]
    if case["flag"] == "none":
        assert not any(c.startswith("creator_fee_") for c in codes)
        assert "Creator fees" not in doc.summary
    else:
        assert case["flag"] in codes
        assert "Creator fees" in doc.summary


def _resolved(mint: str, cf: fees.CreatorFee, curve: dict[str, Any]) -> Resolved:
    """What resolve() builds from a curve and its CreatorFee (mirrors resolver.resolve)."""
    onchain = curve.get("creator")
    if cf.mechanism == "sharing_config":
        creator, kind = cf.admin, "sharing_config"
    elif cf.mechanism == "holder_rewards":
        creator, kind = None, "holder_rewards_pda"
    else:
        creator, kind = onchain, "wallet"
    return Resolved(
        mint=mint,
        token_program="token-2022",
        is_pumpfun=True,
        name="n",
        symbol="s",
        uri=None,
        creator=creator,
        bonding_curve=bonding_curve_pda(mint),
        complete=bool(curve.get("complete")),
        curve_progress=0.5,
        is_mayhem=False,
        quote_mint="SOL",
        created_at=None,
        created_at_source=None,
        onchain_metadata_source="token2022",
        creator_onchain=onchain,
        creator_kind=kind,
        creator_fee=cf,
    )


# ----------------------------------------------------------------- synthetic edge cases


async def test_sharing_config_rpc_failure_is_a_caveat_not_an_error() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    chain.add_sharing_config(T22_MINT, CREATOR, [(WALLET, 10_000)])
    curve = decode_bonding_curve(
        __import__("base64").b64decode(chain.accounts[bonding_curve_pda(T22_MINT)]["data"][0])
    )
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            router.post(RPC).mock(return_value=httpx.Response(503))
            cf = await fees.resolve_creator_fee(
                T22_MINT,
                curve,
                chain.accounts[fees.sharing_config_pda(T22_MINT)],
                rpc=SolanaRpc(RPC, http),
                http=http,
            )
    assert cf.destination == "unknown" and cf.mechanism == "sharing_config"
    assert cf.recipients[0].kind == "unresolved"
    assert any("could not be read" in c for c in cf.caveats)


async def test_github_login_lookup_fills_login_and_url() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    pda = chain.add_social_fee_pda("1234567", 2, total_claimed=5 * 10**9)
    chain.add_sharing_config(T22_MINT, CREATOR, [(CREATOR, 3_000), (pda, 7_000)])
    curve = decode_bonding_curve(
        __import__("base64").b64decode(chain.accounts[bonding_curve_pda(T22_MINT)]["data"][0])
    )
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            router.post(RPC).mock(side_effect=chain.handle)
            gh = router.get("https://api.github.com/user/1234567").mock(
                return_value=httpx.Response(200, json={"login": "octocat", "type": "User"})
            )
            cf = await fees.resolve_creator_fee(
                T22_MINT,
                curve,
                chain.accounts[fees.sharing_config_pda(T22_MINT)],
                rpc=SolanaRpc(RPC, http),
                http=http,
                github_token="tok",
            )
    assert gh.called and gh.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert cf.destination == "github" and cf.split is True
    assert cf.shares == {"creator": 0.3, "github": 0.7}
    r = [x for x in cf.recipients if x.kind == "github"][0]
    assert r.github_login == "octocat" and r.url == "https://github.com/octocat"
    assert r.lifetime_received == 5.0
    assert "70% to GitHub account @octocat" in cf.describe()


async def test_holder_rewards_and_cashback_need_no_sharing_read() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    chain.set_curve(T22_MINT, creator=fees.holder_rewards_pda(T22_MINT), holder_reward=True)
    curve = decode_bonding_curve(
        __import__("base64").b64decode(chain.accounts[bonding_curve_pda(T22_MINT)]["data"][0])
    )
    cf = await fees.resolve_creator_fee(T22_MINT, curve, None, rpc=None)
    assert cf.destination == "holder_rewards" and not cf.recipients and not cf.caveats
    chain.set_curve(T22_MINT, cashback=True)
    curve = decode_bonding_curve(
        __import__("base64").b64decode(chain.accounts[bonding_curve_pda(T22_MINT)]["data"][0])
    )
    cf = await fees.resolve_creator_fee(T22_MINT, curve, None, rpc=None)
    assert cf.destination == "cashback"


async def test_x_and_program_recipients_map_to_social_and_other() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    x_pda = chain.add_social_fee_pda("44196397", 1)
    chain.add_sharing_config(T22_MINT, CREATOR, [(x_pda, 10_000)])
    curve = decode_bonding_curve(
        __import__("base64").b64decode(chain.accounts[bonding_curve_pda(T22_MINT)]["data"][0])
    )
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            router.post(RPC).mock(side_effect=chain.handle)
            sc = chain.accounts[fees.sharing_config_pda(T22_MINT)]
            cf = await fees.resolve_creator_fee(T22_MINT, curve, sc, rpc=SolanaRpc(RPC, http))
            assert cf.destination == "social" and cf.recipients[0].kind == "x"
            assert cf.recipients[0].platform == "x" and cf.recipients[0].user_id == "44196397"
            # a shareholder owned by some other program (a token mint, say)
            chain.add_plain_spl(USDC)
            chain.add_sharing_config(T22_MINT, CREATOR, [(USDC, 10_000)])
            sc = chain.accounts[fees.sharing_config_pda(T22_MINT)]
            cf = await fees.resolve_creator_fee(T22_MINT, curve, sc, rpc=SolanaRpc(RPC, http))
            assert cf.destination == "other" and cf.recipients[0].kind == "program"
    doc = build_document(_resolved(T22_MINT, cf, curve), None, "basic", None, None)
    Analysis.model_validate(doc.model_dump(mode="json"))
    assert "creator_fee_other" in [f.code for f in doc.flags]


def test_describe_sentences() -> None:
    cf = fees.CreatorFee("split", "sharing_config", admin=CREATOR)
    cf.recipients = [
        fees.FeeRecipient(CREATOR, 2_500, "creator", is_creator=True),
        fees.FeeRecipient(WALLET, 7_500, "wallet"),
    ]
    assert cf.describe() == (
        "creator fees are split: 75% to wallet B8wt…95vR, 25% to the creator wallet"
    )
    assert (
        fees.CreatorFee("holder_rewards", "holder_rewards")
        .describe()
        .startswith("creator fees go to the coin's holders")
    )
    assert "could not be determined" in fees.CreatorFee("unknown", "direct").describe()


# ----------------------------------------------------------------- through resolve() and the API


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


@pytest.fixture
async def db(migrated_db: str, clean_tables: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await conn.execute("truncate token_metadata, image, x_ref, token_market, fee_recipient cascade")
    try:
        yield conn
    finally:
        await conn.close()


@needs_db
async def test_resolve_reads_sharing_config_in_the_batched_call_and_caches_recipients(
    db: asyncpg.Connection, settings: Settings
) -> None:
    captured = CAPTURED["github_shelldon"]
    chain = FakeChain()
    chain.add_t22_pump(captured["mint"], "shelldon", "shelldon", META_URI)
    chain.load_captured(captured)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            install_web(router, chain)
            rpc = SolanaRpc(RPC, http)
            r = await resolve(db, rpc, http, settings, captured["mint"])
            # one batched read (mint, curve, Metaplex PDA, sharing config) + one for the
            # shareholder accounts
            assert chain.calls.count("getMultipleAccounts") == 2
            assert r.creator_kind == "sharing_config"
            assert r.creator == "EM2C5hJ1CubYKhikyCR2JiBrLiaWPpZUoXANPZ3PsBsD"  # the admin wallet
            assert r.creator_onchain == fees.sharing_config_pda(captured["mint"])
            assert r.creator_fee is not None and r.creator_fee.destination == "github"
            # the token row stores the wallet, not the PDA
            assert (
                await db.fetchval("select creator from token where mint=$1", captured["mint"])
                == "EM2C5hJ1CubYKhikyCR2JiBrLiaWPpZUoXANPZ3PsBsD"
            )
            cached = await db.fetchrow(
                "select * from fee_recipient where address=$1",
                "9VZA4fKUT5SQXiirfdoFaxpA87bHGBUfmjWxhHFhTkpP",
            )
            assert cached and cached["kind"] == "github" and cached["user_id"] == "258455447"
            # a second resolve re-reads the fee PDA (its claimed total moves) but not GitHub
            chain.calls.clear()
            github_calls = router.calls.call_count
            r2 = await resolve(db, rpc, http, settings, captured["mint"])
            assert chain.calls.count("getMultipleAccounts") == 2
            assert not any(
                "api.github.com" in str(c.request.url) for c in list(router.calls)[github_calls:]
            )
            assert r2.creator_fee is not None and r2.creator_fee.destination == "github"
            assert r2.creator_fee.recipients[0].user_id == "258455447"


@needs_db
async def test_holder_rewards_row_stored_by_old_rules_is_not_reused_as_creator(
    db: asyncpg.Connection, settings: Settings
) -> None:
    captured = CAPTURED["holder_rewards_shitcoin"]
    pda = fees.holder_rewards_pda(captured["mint"])
    await db.execute(
        "insert into token (mint, creator, is_pumpfun) values ($1, $2, true)", captured["mint"], pda
    )
    chain = FakeChain()
    chain.add_t22_pump(captured["mint"], "Shitcoin", "SHITCOIN", META_URI)
    chain.load_captured(captured)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            install_web(router, chain)
            r = await resolve(db, SolanaRpc(RPC, http), http, settings, captured["mint"])
    assert r.creator is None and r.creator_kind == "holder_rewards_pda"
    assert r.creator_fee is not None and r.creator_fee.destination == "holder_rewards"
    assert await db.fetchval("select creator from token where mint=$1", captured["mint"]) is None


@needs_db
async def test_api_reports_creator_fee(client: httpx.AsyncClient, db: asyncpg.Connection) -> None:
    captured = CAPTURED["charity_plus_wallet_kind"]
    chain = FakeChain()
    chain.add_t22_pump(captured["mint"], "KindnessCoin", "KIND", META_URI)
    chain.load_captured(captured)
    with respx.mock(assert_all_called=False) as router:
        install_web(router, chain)
        resp = await client.get(
            f"/v1/tokens/{captured['mint']}", params={"wait": 8, "depth": "basic"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fee = body["analysis"]["market"]["creator_fee"]
    assert fee["destination"] == "charity" and fee["split"] is True
    assert fee["shares"] == {"creator": 0.01, "charity": 0.99}
    assert (
        fee["recipients"][1]["charity_config_id"] == "H16fEMZN9b8Zmhh5Ara343WdZdSdgn2P8oUkAGdB57Ru"
    )
    assert body["analysis"]["market"]["creator"] == "8PQxd6VmfGPMyg8WPnfkT9jUTmtE7UsnDmvBKXeAVP9z"
    assert body["analysis"]["market"]["creator_kind"] == "sharing_config"
    assert "creator_fee_charity" in [f["code"] for f in body["analysis"]["flags"]]
    assert body["analysis"]["versions"]["rules"] == RULES_VERSION
    meta = (await client.get("/v1/meta")).json()
    assert "creator_fee_charity" in [f["code"] for f in meta["flags"]]


# ----------------------------------------------------------------- bugs audit (2026-10-08)


def _curve_of(chain: FakeChain, mint: str) -> dict[str, Any]:
    return decode_bonding_curve(
        base64.b64decode(chain.accounts[bonding_curve_pda(mint)]["data"][0])
    )


async def _resolve_fee(
    chain: FakeChain,
    *,
    rpc_ok: bool = True,
    conn: asyncpg.Connection | None = None,
) -> fees.CreatorFee:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            if rpc_ok:
                router.post(RPC).mock(side_effect=chain.handle)
            else:
                router.post(RPC).mock(return_value=httpx.Response(503))
            return await fees.resolve_creator_fee(
                T22_MINT,
                _curve_of(chain, T22_MINT),
                chain.accounts[fees.sharing_config_pda(T22_MINT)],
                rpc=SolanaRpc(RPC, http),
                conn=conn,
                http=http,
                lookup_github=False,
            )


async def test_failed_recipient_read_on_a_split_is_unknown_not_a_split_flag() -> None:
    # Two shareholders and a failed read used to give destination "split" and the summary
    # "70% to unresolved B8wt…": now no verdict, no flag, no internal kind word.
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    chain.add_sharing_config(T22_MINT, CREATOR, [(CREATOR, 3_000), (WALLET, 7_000)])
    cf = await _resolve_fee(chain, rpc_ok=False)
    assert cf.destination == "unknown" and cf.split is True
    assert "unresolved" not in cf.describe()
    doc = build_document(
        _resolved(T22_MINT, cf, _curve_of(chain, T22_MINT)), None, "basic", None, None
    )
    assert not [f for f in doc.flags if f.code.startswith("creator_fee_")]
    assert "unresolved" not in doc.summary
    # the renderer has a phrase for every kind, even when a caller builds a split by hand
    cf2 = fees.CreatorFee("split", "sharing_config", admin=CREATOR)
    cf2.recipients = [
        fees.FeeRecipient(WALLET, 5_000, "unresolved"),
        fees.FeeRecipient(CREATOR, 5_000, "social", platform="platform:7"),
    ]
    text = cf2.describe()
    assert "unresolved" not in text and "could not be classified" in text
    assert "a linked social account (platform:7)" in text


async def test_uncreated_social_fee_pda_is_not_a_wallet() -> None:
    # A GitHub user's fee PDA that nobody has created yet does not exist on-chain. It is off
    # the ed25519 curve, so it cannot be a wallet.
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    pda = fees.social_fee_pda("7654321", 2)
    chain.add_sharing_config(T22_MINT, CREATOR, [(pda, 10_000)])
    cf = await _resolve_fee(chain)
    assert cf.destination == "unknown" and cf.recipients[0].kind == "unresolved"
    assert any("does not exist yet" in c for c in cf.caveats)
    # a never-funded wallet (on the curve) is still a wallet
    assert fees.classify_recipient_account(WALLET, None) == {"kind": "wallet"}


@needs_db
async def test_uncreated_pda_and_running_totals_are_not_cached(
    db: asyncpg.Connection,
) -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    pda = fees.social_fee_pda("7654321", 2)
    chain.add_sharing_config(T22_MINT, CREATOR, [(pda, 10_000)])
    cf = await _resolve_fee(chain, conn=db)
    assert cf.destination == "unknown"
    assert await db.fetchval("select count(*) from fee_recipient where address=$1", pda) == 0
    # the PDA is created and claims 1 SOL: the next read sees it
    chain.add_social_fee_pda("7654321", 2, total_claimed=10**9)
    cf = await _resolve_fee(chain, conn=db)
    assert cf.destination == "github" and cf.recipients[0].lifetime_received == 1.0
    # it claims 500 SOL more: the total is re-read, not served from the 7-day cache
    chain.add_social_fee_pda("7654321", 2, total_claimed=501 * 10**9)
    chain.calls.clear()
    cf = await _resolve_fee(chain, conn=db)
    assert cf.recipients[0].lifetime_received == 501.0
    assert chain.calls.count("getMultipleAccounts") == 1
    # a plain wallet is cached and not read again
    chain.add_wallet(WALLET)
    chain.add_sharing_config(T22_MINT, CREATOR, [(WALLET, 10_000)])
    await _resolve_fee(chain, conn=db)
    chain.calls.clear()
    cf = await _resolve_fee(chain, conn=db)
    assert cf.destination == "wallet" and chain.calls.count("getMultipleAccounts") == 0


async def test_recipient_read_asks_for_a_data_slice() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    pda = chain.add_social_fee_pda("1234567", 2, total_claimed=10**9)
    chain.add_sharing_config(T22_MINT, CREATOR, [(pda, 10_000)])
    seen: list[dict] = []

    def spy(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "getMultipleAccounts":
            seen.append(body["params"][1])
        return chain.handle(request)

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            router.post(RPC).mock(side_effect=spy)
            cf = await fees.resolve_creator_fee(
                T22_MINT,
                _curve_of(chain, T22_MINT),
                chain.accounts[fees.sharing_config_pda(T22_MINT)],
                rpc=SolanaRpc(RPC, http),
                lookup_github=False,
            )
    assert len(seen) == 1 and seen[0]["dataSlice"] == {"offset": 0, "length": 256}
    # the decoder still reads everything it needs from the first 256 bytes
    assert cf.destination == "github" and cf.recipients[0].lifetime_received == 1.0


async def test_hostile_social_user_id_is_never_echoed() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    hostile = "\x1b[31m<img src=x>\r\n#"
    pda = fees.social_fee_pda(hostile, 2)
    raw = base64.b64encode(social_fee_pda_bytes(hostile, 2)).decode()
    chain.accounts[pda] = {"owner": fees.PUMP_FEES_PROGRAM, "lamports": 1, "data": [raw, "base64"]}
    chain.add_sharing_config(T22_MINT, CREATOR, [(pda, 10_000)])
    cf = await _resolve_fee(chain)
    r = cf.recipients[0]
    assert cf.destination == "github" and r.user_id is None and r.url is None
    assert hostile not in cf.describe() and "\x1b" not in cf.describe()
    assert await fees.github_login(httpx.AsyncClient(), "١٢٣") == (None, None)  # non-ASCII digits


async def test_shares_over_10000_bps_degrade_to_unknown() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    chain.add_sharing_config(T22_MINT, CREATOR, [(CREATOR, 20_000)])
    cf = await fees.resolve_creator_fee(
        T22_MINT,
        _curve_of(chain, T22_MINT),
        chain.accounts[fees.sharing_config_pda(T22_MINT)],
        rpc=None,
    )
    assert cf.destination == "unknown" and any("could not be read" in c for c in cf.caveats)
    doc = build_document(
        _resolved(T22_MINT, cf, _curve_of(chain, T22_MINT)), None, "basic", None, None
    )
    Analysis.model_validate(doc.model_dump(mode="json"))


async def test_odd_account_shapes_never_raise() -> None:
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "n", "s", META_URI)
    pda = fees.sharing_config_pda(T22_MINT)
    chain.set_curve(T22_MINT, creator=pda)
    curve = _curve_of(chain, T22_MINT)
    # the sharing config as a jsonParsed object
    cf = await fees.resolve_creator_fee(
        T22_MINT, curve, {"owner": fees.PUMP_FEES_PROGRAM, "data": {"parsed": {}}}, rpc=None
    )
    assert cf.destination == "unknown"
    # recipient accounts whose data element is not a base64 string
    for data in ([123, "base64"], [None], {"parsed": {}}):
        info = fees.classify_recipient_account(
            WALLET, {"owner": fees.PUMP_FEES_PROGRAM, "data": data}
        )
        assert info["kind"] == "program"
