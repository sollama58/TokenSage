"""Phase 0 smoke test: which external sources work from THIS network?

Run it from a Render shell / one-off job (or locally) and paste the output into
docs/smoke-test-results.md. It needs only the standard library plus httpx.

    SOLANA_RPC_URL=https://... python scripts/smoke_test.py [--cas cas.txt] [--quick]

Every check is independent and reports: works / flaky / blocked / skipped, with latency.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tokensage.engine.xref import syndication_token  # noqa: E402
from tokensage.resolve.pump_ca import bonding_curve_pda, parse_ca  # noqa: E402

UA = os.environ.get("HTTP_USER_AGENT", "TokenSage/0.1 (+https://github.com/sollama58/TokenSage)")
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl-token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "token-2022",
}

# Default test set. Replace/extend with --cas. Mixed on purpose: pump coins (legacy + v2 if
# known), a graduated coin, a non-pump SPL mint (USDC), a wallet-looking address, junk.
DEFAULT_CAS = [
    "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump",  # pump, graduated (frontend-api sample)
    "7YD6ZHb39jGb33ki2iFd7kHK11yMkSFCc9kvjJscpump",  # pump, 2026 CreateEvent sample
    "457V2vvjqXTMFzivq9tvBqhDaxfke2523hHDB6brpump",  # pump, 2026 ("dog wif cap")
    "PuuQ746gUr4mUkMzZhaYkgQomxQNpVgQfdEmZUVpump",  # pump, 2026 ("FOMO")
    "FdCYwtezFn1vhzsSPXbLZiAJungb8LETKSnV1iVdK5Xi",  # pump without 'pump' suffix
    "5hiHBjx5Ah1nAnCiq7mBTnefqkDVcyJwZuWFfZxFnErR",  # pump, non-IPFS metadata host
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC: SPL mint, not pump
    "B8wtc55J62sZ9reiyWLCkJ46b9YnQeMqSbmeyZUg95vR",  # a wallet, not a mint
    "hello-not-an-address",  # junk
]

TWEET_ID = "20"  # jack: "just setting up my twttr"
TWEET_ID_MODERN = "1577730467436138524"  # @Twitter 2022 (has media)
HANDLE = "jack"
COMMUNITY_ID = "1804846498066116981"


@dataclass
class Result:
    source: str
    verdict: str  # works | flaky | blocked | skipped
    latency_ms: int | None = None
    note: str = ""
    details: list[str] = field(default_factory=list)


class Checker:
    def __init__(self, quick: bool):
        self.quick = quick
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "*/*"},
            timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
            follow_redirects=True,
        )
        self.results: list[Result] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, **kw: Any) -> tuple[httpx.Response | None, int, str]:
        t0 = time.perf_counter()
        try:
            r = await self.client.get(url, **kw)
            return r, int((time.perf_counter() - t0) * 1000), ""
        except Exception as e:  # noqa: BLE001
            return None, int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {e}"

    async def _post(self, url: str, body: Any) -> tuple[httpx.Response | None, int, str]:
        t0 = time.perf_counter()
        try:
            r = await self.client.post(url, json=body)
            return r, int((time.perf_counter() - t0) * 1000), ""
        except Exception as e:  # noqa: BLE001
            return None, int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {e}"

    @staticmethod
    def _verdict(oks: int, total: int) -> str:
        if total == 0:
            return "skipped"
        if oks == total:
            return "works"
        if oks == 0:
            return "blocked"
        return "flaky"

    async def simple(self, source: str, url: str, expect: str | None = None, **kw: Any) -> Result:
        """One GET, judged by status 200 and (optionally) a substring in the body."""
        r, ms, err = await self._get(url, **kw)
        if r is None:
            res = Result(source, "blocked", ms, err)
        elif r.status_code == 200 and (expect is None or expect in r.text):
            res = Result(source, "works", ms, f"200, {len(r.content)} bytes")
        elif r.status_code == 200:
            res = Result(source, "flaky", ms, f"200 but body lacks {expect!r}")
        else:
            res = Result(source, "blocked", ms, f"HTTP {r.status_code}: {r.text[:120]!r}")
        self.results.append(res)
        return res

    # ------------------------------------------------------------- Solana RPC

    async def rpc(self, url: str, method: str, params: list[Any]) -> tuple[Any, int, str]:
        r, ms, err = await self._post(
            url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if r is None:
            return None, ms, err
        if r.status_code != 200:
            return None, ms, f"HTTP {r.status_code}: {r.text[:200]}"
        body = r.json()
        if "error" in body:
            return None, ms, f"rpc error: {body['error']}"
        return body.get("result"), ms, ""

    async def check_rpc(self, rpc_url: str, cas: list[str]) -> None:
        if not rpc_url:
            self.results.append(Result("solana.rpc", "skipped", note="SOLANA_RPC_URL not set"))
            return
        res = Result("solana.rpc.getAccountInfo", "skipped")
        oks = total = 0
        lat: list[int] = []
        for raw in cas:
            try:
                ca = parse_ca(raw)
            except ValueError as e:
                res.details.append(f"- `{raw}`: invalid_ca ({e}) [no network]")
                continue
            total += 1
            mint, ms, err = await self.rpc(
                rpc_url,
                "getAccountInfo",
                [ca, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            lat.append(ms)
            if err:
                res.details.append(f"- `{ca}`: ERROR {err}")
                continue
            oks += 1
            if not mint or not mint.get("value"):
                res.details.append(f"- `{ca}`: no account (token_not_found)")
                continue
            owner = mint["value"].get("owner")
            prog = TOKEN_PROGRAMS.get(owner)
            parsed = mint["value"].get("data", {})
            kind = parsed.get("parsed", {}).get("type") if isinstance(parsed, dict) else None
            if prog is None or kind != "mint":
                res.details.append(f"- `{ca}`: owner={owner} type={kind} -> not_a_token_mint")
                continue
            info = parsed["parsed"]["info"]
            name = symbol = uri = None
            for ext in info.get("extensions", []) or []:
                if ext.get("extension") == "tokenMetadata":
                    st = ext.get("state", {})
                    name, symbol, uri = st.get("name"), st.get("symbol"), st.get("uri")
            curve = bonding_curve_pda(ca)
            bc, ms2, err2 = await self.rpc(
                rpc_url,
                "getAccountInfo",
                [curve, {"encoding": "base64", "commitment": "confirmed"}],
            )
            lat.append(ms2)
            if err2:
                res.details.append(f"- `{ca}`: {prog} mint; bonding-curve lookup ERROR {err2}")
                continue
            has_curve = bool(bc and bc.get("value"))
            complete = None
            if has_curve:
                data = base64.b64decode(bc["value"]["data"][0])
                if len(data) >= 8 + 40 + 1:
                    complete = bool(data[8 + 40])
            res.details.append(
                f"- `{ca}`: {prog} mint; pump bonding curve={'yes' if has_curve else 'NO'}"
                + (f" complete={complete}" if has_curve else "")
                + (f"; on-chain name={name!r} symbol={symbol!r} uri={uri!r}" if name else "")
                + ("" if prog == "token-2022" else "; (legacy: name/uri need Metaplex PDA or DAS)")
            )
        res.verdict = self._verdict(oks, total)
        res.latency_ms = int(sum(lat) / len(lat)) if lat else None
        res.note = f"{oks}/{total} CAs answered; avg latency shown"
        self.results.append(res)

        # DAS getAsset (Helius and some others)
        asset, ms, err = await self.rpc(rpc_url, "getAsset", [{"id": cas[0]}])
        if err:
            self.results.append(Result("solana.rpc.getAsset(DAS)", "blocked", ms, err[:160]))
        else:
            c = (asset or {}).get("content", {})
            self.results.append(
                Result(
                    "solana.rpc.getAsset(DAS)",
                    "works",
                    ms,
                    f"name={c.get('metadata', {}).get('name')!r} uri={c.get('json_uri')!r}",
                )
            )

        # creation-time via history: how many signatures does the curve have?
        curve = bonding_curve_pda(cas[0])
        sigs, ms, err = await self.rpc(
            rpc_url, "getSignaturesForAddress", [curve, {"limit": 1000, "commitment": "confirmed"}]
        )
        if err:
            self.results.append(
                Result("solana.rpc.getSignaturesForAddress", "blocked", ms, err[:160])
            )
        else:
            n = len(sigs or [])
            self.results.append(
                Result(
                    "solana.rpc.getSignaturesForAddress",
                    "works",
                    ms,
                    f"{n} sigs in first page for {cas[0][:8]}… "
                    + ("(>=1000: creation-time fallback needs paging)" if n >= 1000 else ""),
                )
            )

    # ------------------------------------------------------------- IPFS

    async def check_ipfs(self, uris: list[str]) -> None:
        gateways = os.environ.get(
            "IPFS_GATEWAYS",
            "https://pump.mypinata.cloud,https://dweb.link,https://ipfs.io,https://gateway.pinata.cloud",
        ).split(",")
        cids = []
        for u in uris:
            m = re.search(r"/ipfs/([A-Za-z0-9]+)", u)
            if m:
                cids.append(m.group(1))
        if not cids:
            cids = ["bafkreignns4pa47e6yy3jiw7ua34gl3tagb4k2rmuxgly32zeku4abiukm"]
        for gw in gateways:
            gw = gw.strip().rstrip("/")
            oks, lat = 0, []
            notes = []
            for cid in cids[: 2 if self.quick else 5]:
                r, ms, err = await self._get(f"{gw}/ipfs/{cid}")
                lat.append(ms)
                if r is not None and r.status_code == 200:
                    oks += 1
                else:
                    notes.append(err or f"HTTP {r.status_code}" if r else err)
            n = len(cids[: 2 if self.quick else 5])
            self.results.append(
                Result(
                    f"ipfs.{gw.split('//')[1]}",
                    self._verdict(oks, n),
                    int(sum(lat) / len(lat)) if lat else None,
                    f"{oks}/{n} CIDs" + (f"; {notes[0][:80]}" if notes else ""),
                )
            )

    # ------------------------------------------------------------- pump.fun / dex

    async def check_pumpfun(self, ca: str) -> None:
        await self.simple(
            "pumpfun.frontend-api-v3.coins-v2",
            f"https://frontend-api-v3.pump.fun/coins-v2/{ca}",
            expect='"mint"',
        )
        await self.simple(
            "pumpfun.frontend-api-v3.coins.search",
            "https://frontend-api-v3.pump.fun/coins/search?searchTerm=pnut&limit=10&offset=0",
            expect="[",
        )
        await self.simple(
            "dexscreener.search",
            "https://api.dexscreener.com/latest/dex/search?q=PNUT",
            expect='"pairs"',
        )
        await self.simple(
            "dexscreener.tokens",
            f"https://api.dexscreener.com/tokens/v1/solana/{ca}",
            expect="[",
        )

    # ------------------------------------------------------------- X / Twitter

    async def check_x(self) -> None:
        await self.simple(
            "x.fxtwitter.status",
            f"https://api.fxtwitter.com/2/status/{TWEET_ID_MODERN}",
            expect='"status"',
        )
        await self.simple(
            "x.fxtwitter.profile", f"https://api.fxtwitter.com/2/profile/{HANDLE}", expect='"user"'
        )
        await self.simple(
            "x.fxtwitter.profile.about",
            f"https://api.fxtwitter.com/2/profile/{HANDLE}/about",
            expect="{",
        )
        await self.simple(
            "x.vxtwitter.status",
            f"https://api.vxtwitter.com/i/status/{TWEET_ID_MODERN}",
            expect='"text"',
        )
        await self.simple("x.vxtwitter.profile", f"https://api.vxtwitter.com/{HANDLE}", expect="{")
        tok = syndication_token(TWEET_ID)
        await self.simple(
            "x.syndication.tweet-result",
            f"https://cdn.syndication.twimg.com/tweet-result?id={TWEET_ID}&lang=en&token={tok}",
            expect="twttr",
        )
        await self.simple(
            "x.oembed",
            f"https://publish.x.com/oembed?url=https://x.com/{HANDLE}/status/{TWEET_ID}&omit_script=1&dnt=true",
            expect="twttr",
        )
        # untested idea from research: crawler UA on a community page -> og meta?
        r, ms, err = await self._get(
            f"https://x.com/i/communities/{COMMUNITY_ID}", headers={"User-Agent": "Twitterbot/1.0"}
        )
        if r is None:
            self.results.append(Result("x.community.crawler-ua", "blocked", ms, err))
        else:
            m = re.search(r"<title>([^<]*)</title>", r.text)
            og = re.search(r'og:description" content="([^"]*)', r.text)
            found = bool(m and "Community" in m.group(1))
            self.results.append(
                Result(
                    "x.community.crawler-ua",
                    "works" if found else "blocked",
                    ms,
                    f"HTTP {r.status_code}; title={m.group(1)[:80] if m else None!r}"
                    + (f"; og={og.group(1)[:80]!r}" if og else ""),
                )
            )

    # ------------------------------------------------------------- knowledge

    async def check_knowledge(self) -> None:
        key = os.environ.get("COINGECKO_API_KEY", "")
        hdr = {"x-cg-demo-api-key": key} if key else {}
        await self.simple(
            "coingecko.categories.list",
            "https://api.coingecko.com/api/v3/coins/categories/list",
            expect="pump",
            headers=hdr,
        )
        await self.simple(
            "coingecko.search",
            "https://api.coingecko.com/api/v3/search?query=dogwifhat",
            expect='"coins"',
            headers=hdr,
        )
        import datetime as dt

        d = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
        await self.simple(
            "wikimedia.pageviews.top",
            f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{d:%Y/%m/%d}",
            expect='"articles"',
        )
        await self.simple(
            "wikipedia.search",
            "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=peanut%20squirrel&format=json",
            expect='"search"',
        )
        await self.simple(
            "wikidata.search",
            "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=dogwifhat&language=en&format=json",
            expect='"search"',
        )
        await self.simple(
            "googlenews.rss",
            "https://news.google.com/rss/search?q=solana+when:2d&hl=en-US&gl=US&ceid=US:en",
            expect="<rss",
        )
        await self.simple(
            "urbandictionary.define",
            "https://api.urbandictionary.com/v0/define?term=wagmi",
            expect='"list"',
        )


def render(results: list[Result], meta: dict[str, Any]) -> str:
    icon = {"works": "✅", "flaky": "⚠️", "blocked": "❌", "skipped": "⏭️"}
    out = [
        "# Smoke test results",
        "",
        f"- run at: {meta['when']}",
        f"- from: {meta['where']}",
        f"- rpc configured: {'yes' if meta['rpc'] else 'no'}",
        "",
        "| Source | Verdict | Latency (ms) | Note |",
        "|---|---|---|---|",
    ]
    for r in results:
        note = r.note.replace("|", "/")
        out.append(
            f"| `{r.source}` | {icon[r.verdict]} {r.verdict} | {r.latency_ms or ''} | {note} |"
        )
    for r in results:
        if r.details:
            out += ["", f"## {r.source}", ""] + r.details
    return "\n".join(out) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cas", help="file with one CA per line (default: built-in list)")
    ap.add_argument("--quick", action="store_true", help="fewer CIDs per gateway")
    ap.add_argument("--json", action="store_true", help="also dump JSON to stderr")
    args = ap.parse_args()

    cas = DEFAULT_CAS
    if args.cas:
        cas = [ln.strip() for ln in Path(args.cas).read_text().splitlines() if ln.strip()]
    rpc_url = os.environ.get("SOLANA_RPC_URL", "")

    c = Checker(quick=args.quick)
    try:
        await c.check_rpc(rpc_url, cas)
        uris = [
            "https://ipfs.io/ipfs/bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
            "https://ipfs.io/ipfs/bafkreiguttiiufcl24xtnzgxvvonh7b535a3dh6txrjgwax2hyecsyj46i",
            "https://ipfs.io/ipfs/bafkreigqkxsibtz5tgelqecagzho66whmqdysrxxcorgfnfiucvprkxvzi",
            "https://ipfs.io/ipfs/bafkreidcgli7fysduczbiah3wy4m7ndo7wugr7rys622ef7w57vo4ytobu",
        ]
        await c.check_ipfs(uris)
        await c.check_pumpfun(cas[0])
        await c.check_x()
        await c.check_knowledge()
    finally:
        await c.close()

    meta = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "where": os.environ.get("RENDER_SERVICE_NAME", "local")
        + (f" ({os.environ.get('RENDER_INSTANCE_ID')})" if os.environ.get("RENDER") else ""),
        "rpc": bool(rpc_url),
    }
    print(render(c.results, meta))
    if args.json:
        print(json.dumps([r.__dict__ for r in c.results], indent=1), file=sys.stderr)
    blocked = sum(1 for r in c.results if r.verdict == "blocked")
    return 0 if blocked == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
