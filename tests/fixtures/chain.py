"""A fake Solana chain + fake web for tests: builds realistic RPC responses and routes them
through respx. Shapes follow real getAccountInfo(jsonParsed/base64) payloads."""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

import httpx
import respx

from tokensage.resolve import fees, metaplex
from tokensage.resolve.pump_ca import BONDING_CURVE_DISC, PUMP_PROGRAM, b58decode, bonding_curve_pda
from tokensage.resolve.pump_event import b58encode
from tokensage.resolve.resolver import GLOBAL_DISC, SPL_TOKEN, TOKEN_2022, _global_pda

RPC = "https://rpc.test/"
GW1, GW2 = "https://gw1.test", "https://gw2.test"
CID_META = "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe"
CID_IMG = "bafkreignns4pa47e6yy3jiw7ua34gl3tagb4k2rmuxgly32zeku4abiukm"


def _png() -> bytes:
    """A real 64x64 two-colour PNG so the image stage can hash it."""
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (64, 64), (200, 120, 40))
    ImageDraw.Draw(im).ellipse((12, 12, 52, 52), fill=(250, 220, 40))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


PNG = _png()

T22_MINT = "457V2vvjqXTMFzivq9tvBqhDaxfke2523hHDB6brpump"  # token-2022 pump coin
SPL_MINT = "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump"  # legacy pump coin, graduated
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # SPL mint, not pump
WALLET = "B8wtc55J62sZ9reiyWLCkJ46b9YnQeMqSbmeyZUg95vR"  # system account
MISSING = "5hiHBjx5Ah1nAnCiq7mBTnefqkDVcyJwZuWFfZxFnErR"  # no account
CREATOR = "CTfBTtxhtAyGEysdjp9owVQvjSwPjtrzEGB9ZgAKewsY"

INITIAL_REAL = 793_100_000_000_000


def b64(b: bytes) -> list[str]:
    return [base64.b64encode(b).decode(), "base64"]


def curve_bytes(
    real_token_reserves: int,
    complete: bool,
    creator: str = CREATOR,
    quote: str | None = None,
    *,
    cashback: bool = False,
    holder_reward: bool = False,
    creator_fee_bps: int = 0,
) -> bytes:
    body = struct.pack("<QQQQQ?", 1_000, 30_000_000_000, real_token_reserves, 0, 10**15, complete)
    body += b58decode(creator) + struct.pack("<??", False, cashback)
    body += b58decode(quote) if quote else bytes(32)
    body += struct.pack("<Q??", creator_fee_bps, False, holder_reward)
    return BONDING_CURVE_DISC + body


# --- pump-fees accounts (creator-fee redirects, rules 0.19.0) ---


def sharing_config_bytes(
    mint: str, admin: str, shareholders: list[tuple[str, int]], *, revoked: bool = True
) -> bytes:
    body = struct.pack("<BBB", 255, 2, 1) + b58decode(mint) + b58decode(admin)
    body += struct.pack("<?I", revoked, len(shareholders))
    for addr, bps in shareholders:
        body += b58decode(addr) + struct.pack("<H", bps)
    return fees.SHARING_CONFIG_DISC + body + bytes(1024 - 8 - len(body))


def social_fee_pda_bytes(user_id: str, platform: int, total_claimed: int = 0) -> bytes:
    uid = user_id.encode()
    body = struct.pack("<BB", 255, 1) + struct.pack("<I", len(uid)) + uid
    body += struct.pack("<BQQQ", platform, total_claimed, 0, 0) + bytes(120)
    return fees.SOCIAL_FEE_PDA_DISC + body


def donation_fee_pda_bytes(
    mint: str, config_id: str, creator: str, total_donated: int = 0
) -> bytes:
    body = struct.pack("<BB", 255, 1) + b58decode(config_id) + b58decode(mint)
    body += b58decode("So11111111111111111111111111111111111111112") + b58decode(creator)
    body += struct.pack("<Qq", total_donated, 0) + bytes(64)
    return fees.DONATION_FEE_PDA_DISC + body


def global_bytes() -> bytes:
    return (
        GLOBAL_DISC
        + b"\x01"
        + bytes(64)
        + struct.pack("<QQQQ", 1_073_000_000_000_000, 30_000_000_000, INITIAL_REAL, 10**15)
    )


def acct(owner: str, data: Any, lamports: int = 1_000_000) -> dict:
    return {
        "lamports": lamports,
        "owner": owner,
        "executable": False,
        "rentEpoch": 0,
        "space": 0,
        "data": data,
    }


def parsed_mint(program: str, extensions: list[dict] | None = None) -> dict:
    info: dict[str, Any] = {
        "decimals": 6,
        "isInitialized": True,
        "mintAuthority": None,
        "freezeAuthority": None,
        "supply": "1000000000000000",
    }
    if extensions is not None:
        info["extensions"] = extensions
    return acct(program, {"program": "spl-token", "parsed": {"type": "mint", "info": info}})


def t22_metadata_ext(name: str, symbol: str, uri: str) -> dict:
    return {
        "extension": "tokenMetadata",
        "state": {
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "mint": T22_MINT,
            "updateAuthority": CREATOR,
            "additionalMetadata": [],
        },
    }


class FakeChain:
    """Accounts keyed by address. jsonParsed and base64 encodings both served."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.signatures: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.accounts[_global_pda()] = acct(PUMP_PROGRAM, b64(global_bytes()))

    def add_t22_pump(
        self,
        mint: str,
        name: str,
        symbol: str,
        uri: str,
        progress: float = 0.4,
        quote: str | None = None,
    ) -> None:
        self.accounts[mint] = parsed_mint(TOKEN_2022, [t22_metadata_ext(name, symbol, uri)])
        real = int(INITIAL_REAL * (1 - progress))
        self.accounts[bonding_curve_pda(mint)] = acct(
            PUMP_PROGRAM, b64(curve_bytes(real, False, quote=quote))
        )

    def add_spl_pump(
        self, mint: str, name: str, symbol: str, uri: str, complete: bool = True
    ) -> None:
        self.accounts[mint] = parsed_mint(SPL_TOKEN)
        self.accounts[metaplex.metadata_pda(mint)] = acct(
            metaplex.METAPLEX_PROGRAM, b64(metaplex.encode_metadata_for_tests(name, symbol, uri))
        )
        self.accounts[bonding_curve_pda(mint)] = acct(
            PUMP_PROGRAM, b64(curve_bytes(0 if complete else 10**14, complete))
        )

    def add_plain_spl(self, mint: str) -> None:
        self.accounts[mint] = parsed_mint(SPL_TOKEN)

    def add_spl_token(self, mint: str, name: str, symbol: str, uri: str = "") -> None:
        """A plain (non-pump) SPL mint with Metaplex metadata, e.g. a pair token."""
        self.accounts[mint] = parsed_mint(SPL_TOKEN)
        self.accounts[metaplex.metadata_pda(mint)] = acct(
            metaplex.METAPLEX_PROGRAM, b64(metaplex.encode_metadata_for_tests(name, symbol, uri))
        )

    def add_wallet(self, addr: str) -> None:
        self.accounts[addr] = acct("11111111111111111111111111111111", ["", "base64"])

    # --- creator-fee redirects (rules 0.19.0) ---

    def set_curve(self, mint: str, **curve_kw: Any) -> None:
        """Replace a pump coin's bonding curve (e.g. creator=<PDA>, holder_reward=True)."""
        self.accounts[bonding_curve_pda(mint)] = acct(
            PUMP_PROGRAM, b64(curve_bytes(curve_kw.pop("real", 10**14), False, **curve_kw))
        )

    def add_sharing_config(
        self,
        mint: str,
        admin: str,
        shareholders: list[tuple[str, int]],
        *,
        revoked: bool = True,
        point_curve: bool = True,
    ) -> str:
        """A fee-sharing config for `mint`; by default the curve's creator is re-pointed at it,
        as create_fee_sharing_config does on-chain."""
        pda = fees.sharing_config_pda(mint)
        self.accounts[pda] = acct(
            fees.PUMP_FEES_PROGRAM,
            b64(sharing_config_bytes(mint, admin, shareholders, revoked=revoked)),
        )
        if point_curve:
            self.set_curve(mint, creator=pda)
        return pda

    def add_social_fee_pda(self, user_id: str, platform: int = 2, total_claimed: int = 0) -> str:
        pda = fees.social_fee_pda(user_id, platform)
        self.accounts[pda] = acct(
            fees.PUMP_FEES_PROGRAM, b64(social_fee_pda_bytes(user_id, platform, total_claimed))
        )
        return pda

    def add_donation_fee_pda(
        self, mint: str, config_id: str, creator: str, total_donated: int = 0
    ) -> str:
        pda = fees.donation_fee_pda(mint, config_id)
        self.accounts[pda] = acct(
            fees.PUMP_FEES_PROGRAM,
            b64(donation_fee_pda_bytes(mint, config_id, creator, total_donated)),
        )
        return pda

    def load_captured(self, case: dict[str, Any]) -> None:
        """Install the real mainnet accounts of one tests/fixtures/fee_accounts.json case."""
        for addr, a in case["accounts"].items():
            if a is None:
                continue
            self.accounts[addr] = acct(a["owner"], [a["data"], "base64"], lamports=a["lamports"])

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        rid = body["id"]
        if method == "getAccountInfo":
            a = self.accounts.get(params[0])
            enc = params[1].get("encoding")
            if a is not None and enc == "base64" and isinstance(a["data"], dict):
                a = {**a, "data": ["", "base64"]}  # parsed-only account asked raw: fine
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "result": {"context": {"slot": 1}, "value": a}},
            )
        if method == "getMultipleAccounts":
            # jsonParsed falls back to base64 for accounts with no parser, as stored here
            value = []
            for addr in params[0]:
                a = self.accounts.get(addr)
                if (
                    a is not None
                    and params[1].get("encoding") == "base64"
                    and isinstance(a["data"], dict)
                ):
                    a = {**a, "data": ["", "base64"]}
                value.append(a)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"context": {"slot": 1}, "value": value},
                },
            )
        if method == "getSignaturesForAddress":
            sigs = self.signatures.get(params[0], [])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": sigs})
        if method == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": None})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": "Method not found"},
            },
        )


def metadata_json(**over: Any) -> bytes:
    d = {
        "name": "dog wif cap",
        "symbol": "cap",
        "description": "just a dog wif a cap",
        "image": f"https://ipfs.io/ipfs/{CID_IMG}",
        "showName": True,
        "createdOn": "https://pump.fun",
        "twitter": "https://x.com/elonmusk/status/1791351500217754008?s=20",
        "telegram": "https://t.me/dogwifcap",
        "website": "javascript:alert(1)",
    }
    d.update(over)
    return json.dumps(d).encode()


async def public_resolver(host: str, port: int, **_: Any) -> list[Any]:
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def install_web(
    router: respx.MockRouter,
    chain: FakeChain,
    *,
    gateways_ok: bool = True,
    meta: bytes | None = None,
) -> None:
    router.post(RPC).mock(side_effect=chain.handle)
    if gateways_ok:
        for gw in (GW1, GW2):
            router.get(f"{gw}/ipfs/{CID_META}").mock(
                return_value=httpx.Response(
                    200,
                    content=meta if meta is not None else metadata_json(),
                    headers={"content-type": "application/json"},
                )
            )
            router.get(f"{gw}/ipfs/{CID_IMG}").mock(
                return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            )
    else:
        router.get(url__regex=r"https://gw[12]\.test/.*").mock(return_value=httpx.Response(503))
    router.get(url__regex=r"https://frontend-api-v3\.pump\.fun/.*").mock(
        return_value=httpx.Response(403)
    )
    router.route().mock(return_value=httpx.Response(404))


_ = b58encode  # re-exported for tests that want it
