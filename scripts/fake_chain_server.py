"""DEV ONLY: a local fake Solana RPC + IPFS gateway so the whole stack runs offline.

    uv run python scripts/fake_chain_server.py --port 9999 --tokens 200

Then run the API with:
    SOLANA_RPC_URL=http://127.0.0.1:9999/ IPFS_GATEWAYS=http://127.0.0.1:9999 \
    DEV_ALLOW_INSECURE_FETCH=true INLINE_ANALYZER=true ...

It mints N synthetic pump.fun tokens with varied names (copycats, templates, scripts) so the
load test exercises the engine. Never deploy this.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fixtures.chain import CID_IMG, CID_META, PNG, FakeChain, metadata_json  # noqa: E402
from tokensage.resolve.pump_event import b58encode  # noqa: E402

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
NAMES = [
    ("Peanut the Squirrel 2.0", "PNUT2"),
    ("dog wif cap", "cap"),
    ("Trump wif Hat", "TWIF"),
    ("Baby PNUT", "BPNUT"),
    ("Moo Deng Classic", "MOODENG"),
    ("Just a chill guy", "CHILLGUY"),
    ("AIAgentSupercycle", "AIAS"),
    ("币圈大哥", "BQDG"),
    ("Zxqv Plorth", "ZXQV"),
    ("Corgi Coin", "CORGI"),
    ("Luigi Mangione", "LUIGI"),
    ("Fartcoin", "FARTCOIN"),
    ("Skibidi Toilet", "SKIBIDI"),
    ("Elons Dog", "FLOKI"),
    ("Hawk Tuah", "HAWK"),
    ("WAGMI", "WAGMI"),
    ("Pizza Time", "PIZZA"),
    ("Рepe", "PEPE"),
    ("Quokka", "QUOK"),
    ("Goatseus Maximus", "GOAT"),
]


def mint_for(i: int) -> str:
    rnd = random.Random(1000 + i)
    return b58encode(bytes(rnd.getrandbits(8) for _ in range(32)))


def build(n: int) -> tuple[FakeChain, dict[str, bytes], list[str]]:
    chain = FakeChain()
    metas: dict[str, bytes] = {}
    mints: list[str] = []
    for i in range(n):
        name, sym = NAMES[i % len(NAMES)]
        if i >= len(NAMES):
            name, sym = f"{name} {i}", f"{sym}{i % 7}"
        m = mint_for(i)
        # a syntactically valid base32 CIDv1 (alphabet a-z2-7) so the IPFS parser accepts it
        idx = "".join(ALPHABET[(i >> (5 * k)) & 31] for k in range(4))
        cid = ("bafkreifake" + idx + ALPHABET * 2)[:59]
        uri = f"https://ipfs.io/ipfs/{cid}"
        if i % 2:
            chain.add_t22_pump(m, name, sym, uri, progress=random.Random(i).random())
        else:
            chain.add_spl_pump(m, name, sym, uri, complete=bool(i % 5 == 0))
        metas[cid] = metadata_json(
            name=name,
            symbol=sym,
            description=f"{name} on pump.fun",
            image=f"https://ipfs.io/ipfs/{CID_IMG}",
        )
        mints.append(m)
    metas[CID_META] = metadata_json()
    return chain, metas, mints


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--tokens", type=int, default=200)
    ap.add_argument("--write-cas", default="load_cas.txt")
    args = ap.parse_args()
    chain, metas, mints = build(args.tokens)
    Path(args.write_cas).write_text("\n".join(mints) + "\n")
    print(f"wrote {len(mints)} CAs to {args.write_cas}")

    app = FastAPI()

    @app.post("/")
    async def rpc(req: Request) -> Response:
        r = chain.handle(httpx.Request("POST", "http://x/", content=await req.body()))
        return Response(content=r.content, media_type="application/json")

    @app.get("/ipfs/{cid}")
    async def ipfs(cid: str) -> Response:
        if cid == CID_IMG:
            return Response(content=PNG, media_type="image/png")
        if cid in metas:
            return Response(content=metas[cid], media_type="application/json")
        return Response(status_code=404)

    @app.get("/healthz")
    async def health() -> dict[str, int]:
        return {"tokens": len(mints)}

    json.dumps({"ok": True})
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
