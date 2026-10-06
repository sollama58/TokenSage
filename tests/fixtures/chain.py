"""A fake Solana chain + fake web for tests: builds realistic RPC responses and routes them
through respx. Shapes follow real getAccountInfo(jsonParsed/base64) payloads."""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

import httpx
import respx

from tokensage.resolve import metaplex
from tokensage.resolve.pump_ca import BONDING_CURVE_DISC, PUMP_PROGRAM, b58decode, bonding_curve_pda
from tokensage.resolve.pump_event import b58encode
from tokensage.resolve.resolver import GLOBAL_DISC, SPL_TOKEN, TOKEN_2022, _global_pda

RPC = "https://rpc.test/"
GW1, GW2 = "https://gw1.test", "https://gw2.test"
CID_META = "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe"
CID_IMG = "bafkreignns4pa47e6yy3jiw7ua34gl3tagb4k2rmuxgly32zeku4abiukm"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

T22_MINT = "457V2vvjqXTMFzivq9tvBqhDaxfke2523hHDB6brpump"  # token-2022 pump coin
SPL_MINT = "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump"  # legacy pump coin, graduated
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # SPL mint, not pump
WALLET = "B8wtc55J62sZ9reiyWLCkJ46b9YnQeMqSbmeyZUg95vR"  # system account
MISSING = "5hiHBjx5Ah1nAnCiq7mBTnefqkDVcyJwZuWFfZxFnErR"  # no account
CREATOR = "CTfBTtxhtAyGEysdjp9owVQvjSwPjtrzEGB9ZgAKewsY"

INITIAL_REAL = 793_100_000_000_000


def b64(b: bytes) -> list[str]:
    return [base64.b64encode(b).decode(), "base64"]


def curve_bytes(real_token_reserves: int, complete: bool, creator: str = CREATOR) -> bytes:
    body = struct.pack("<QQQQQ?", 1_000, 30_000_000_000, real_token_reserves, 0, 10**15, complete)
    body += b58decode(creator) + struct.pack("<??", False, False) + bytes(32)
    body += struct.pack("<Q??", 0, False, False)
    return BONDING_CURVE_DISC + body


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
        self, mint: str, name: str, symbol: str, uri: str, progress: float = 0.4
    ) -> None:
        self.accounts[mint] = parsed_mint(TOKEN_2022, [t22_metadata_ext(name, symbol, uri)])
        real = int(INITIAL_REAL * (1 - progress))
        self.accounts[bonding_curve_pda(mint)] = acct(PUMP_PROGRAM, b64(curve_bytes(real, False)))

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

    def add_wallet(self, addr: str) -> None:
        self.accounts[addr] = acct("11111111111111111111111111111111", ["", "base64"])

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


def install_web(router: respx.MockRouter, chain: FakeChain, *, gateways_ok: bool = True) -> None:
    router.post(RPC).mock(side_effect=chain.handle)
    if gateways_ok:
        for gw in (GW1, GW2):
            router.get(f"{gw}/ipfs/{CID_META}").mock(
                return_value=httpx.Response(
                    200, content=metadata_json(), headers={"content-type": "application/json"}
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
