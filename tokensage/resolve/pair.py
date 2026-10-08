"""Look up the token a coin is paired against (the bonding curve's quote mint).

SOL and stablecoins are answered locally. Any other mint is identified, in order, by a
stored TokenSage analysis of it, a token row we already hold, or its on-chain metadata;
the name / symbol is cached in pair_token so popular pair tokens cost one RPC read a week.
Each answer also says whether the pair token is itself a pump.fun coin (pump.fun pairs a
coin with any other pump.fun coin, not just SOL, stablecoins and a few majors).
"""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import structlog

from tokensage.engine.context import ReferentCandidate
from tokensage.engine.pairing import PairInput, neutral
from tokensage.resolve.metadata import _clean_str
from tokensage.resolve.resolver import read_pair_mint
from tokensage.resolve.rpc import RpcError, SolanaRpc

log = structlog.get_logger("pair")

CACHE_TTL = timedelta(days=7)
MISS_TTL = timedelta(hours=1)  # a failed or empty read is retried sooner


async def lookup(
    conn: asyncpg.Connection, rpc: SolanaRpc | None, quote: str | None
) -> PairInput | None:
    """quote: Resolved.quote_mint ("SOL" or a mint address). None when unknown."""
    if not quote:
        return None
    n = neutral(quote)
    if n is not None:
        return n
    pair = PairInput(mint=quote, kind="token")
    await _from_analysis(conn, pair)
    if pair.name or pair.symbol:
        return pair
    row = await conn.fetchrow(
        """select name, symbol, is_pumpfun from token
            where mint=$1 and (name is not null or symbol is not null)""",
        quote,
    )
    if row:
        pair.name, pair.symbol, pair.source = row["name"], row["symbol"], "db"
        pair.pumpfun = row["is_pumpfun"]
        return pair
    # rows cached before is_pumpfun existed are read again, once
    cached = await conn.fetchrow(
        """select name, symbol, source, is_pumpfun from pair_token where mint=$1
           and (is_pumpfun is not null or source = 'none')
           and fetched_at > now() - make_interval(
                 secs => case when source = 'none' then $2::float8 else $3::float8 end)""",
        quote,
        MISS_TTL.total_seconds(),
        CACHE_TTL.total_seconds(),
    )
    if cached:
        pair.name, pair.symbol, pair.source = cached["name"], cached["symbol"], cached["source"]
        pair.pumpfun = cached["is_pumpfun"]
        return pair
    meta = None
    if rpc is not None:
        try:
            meta, pair.pumpfun = await read_pair_mint(rpc, quote)
        except RpcError as e:
            log.info("pair.metadata_failed", mint=quote, error=str(e)[:120])
    pair.name = _clean_str((meta or {}).get("name"), 200)
    pair.symbol = _clean_str((meta or {}).get("symbol"), 32)
    pair.source = "onchain" if (pair.name or pair.symbol) else "none"
    await conn.execute(
        """insert into pair_token (mint, name, symbol, source, is_pumpfun)
           values ($1, $2, $3, $4, $5)
           on conflict (mint) do update set name=excluded.name, symbol=excluded.symbol,
             source=excluded.source, is_pumpfun=excluded.is_pumpfun, fetched_at=now()""",
        quote,
        pair.name,
        pair.symbol,
        pair.source,
        pair.pumpfun,
    )
    return pair


async def _from_analysis(conn: asyncpg.Connection, pair: PairInput) -> None:
    """Fill name / symbol / referent / categories from our newest analysis of the pair token."""
    doc = await conn.fetchval(
        "select doc from analysis where mint=$1 order by version desc limit 1", pair.mint
    )
    if not isinstance(doc, dict):
        return
    raw = doc.get("raw") or {}
    norm = doc.get("normalized") or {}
    pair.name = _clean_str(raw.get("name"), 200)
    pair.symbol = _clean_str(raw.get("symbol"), 32) or norm.get("ticker")
    if not (pair.name or pair.symbol):
        return
    pair.source = "analysis"
    pair.pumpfun = doc.get("launchpad") == "pump.fun"
    ref = doc.get("referent")
    if isinstance(ref, dict) and ref.get("label"):
        pair.referent = ReferentCandidate(
            label=str(ref["label"]),
            kind=str(ref.get("kind") or "other"),
            desc=ref.get("desc"),
            source=f"analysis:{pair.mint}",
            score=float(ref.get("confidence") or 0.0),
        )
    pair.categories = [
        (str(c["label"]), float(c.get("confidence") or 0.0))
        for c in doc.get("categories") or []
        if isinstance(c, dict) and c.get("label")
    ]
