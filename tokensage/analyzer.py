"""The analyzer entry point the worker calls per job.

Phase 2: resolve the CA on-chain, fetch the off-chain metadata and image header, parse the
X link, and write an Analysis with real `raw`, `market`, `x.ref` and flags. The engine
stages (S1–S10, guide §5) arrive in Phase 3; until then `summary` says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import httpx
import structlog

from tokensage.api.schemas import (
    Analysis,
    Evidence,
    Flag,
    ImageInfo,
    Market,
    RawFields,
    Versions,
    XInfo,
    XRef,
)
from tokensage.config import Settings
from tokensage.engine.xref import parse_x_ref, snowflake_time
from tokensage.resolve import metadata as md
from tokensage.resolve.resolver import Resolved, ResolveError, resolve
from tokensage.resolve.rpc import SolanaRpc

log = structlog.get_logger("analyzer")

RULES_VERSION = "0.1.0-resolve"  # bumps when stage logic changes
LEXICON_VERSION = "2026-10-06"


class AnalyzeFailed(Exception):
    """Definitive failure with a machine-readable code (maps to an API error)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Context:
    """Shared clients for one worker process."""

    settings: Settings
    http: httpx.AsyncClient
    rpc: SolanaRpc | None

    @classmethod
    def create(cls, settings: Settings) -> Context:
        http = httpx.AsyncClient(
            headers={"User-Agent": settings.http_user_agent},
            timeout=httpx.Timeout(
                connect=settings.fetch_connect_timeout_s,
                read=settings.fetch_total_timeout_s,
                write=settings.fetch_connect_timeout_s,
                pool=settings.fetch_connect_timeout_s,
            ),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        rpc = SolanaRpc(settings.solana_rpc_url, http) if settings.solana_rpc_url else None
        return cls(settings=settings, http=http, rpc=rpc)

    async def close(self) -> None:
        await self.http.aclose()


# ----------------------------------------------------------------- building the document


def _x_info(twitter: str | None, token_created: datetime | None) -> XInfo | None:
    ref = parse_x_ref(twitter)
    if ref["kind"] == "empty":
        return None
    xr = XRef(
        kind=ref["kind"],
        tweet_id=ref.get("tweet_id"),
        community_id=ref.get("community_id"),
        url_handle=ref.get("url_handle"),
        handle=ref.get("handle"),
        user_id=ref.get("user_id"),
        query=ref.get("query"),
        url=ref.get("url"),
    )
    obj_time = None
    obj_id = ref.get("tweet_id") or ref.get("community_id")
    if obj_id:
        try:
            obj_time = snowflake_time(obj_id)
        except (ValueError, OverflowError, OSError):
            obj_time = None
    predates = None
    if obj_time and token_created:
        predates = int((token_created - obj_time).total_seconds())
    return XInfo(
        ref=xr,
        object_time=obj_time,
        predates_token_by_s=predates,
        relation="search_only" if ref["kind"] == "search" else None,
        status="not_fetched" if ref["kind"] in ("tweet", "profile", "community") else "none",
    )


def build_document(r: Resolved, m: md.Metadata | None, depth: str) -> Analysis:
    now = datetime.now(UTC)
    flags: list[Flag] = []
    caveats: list[str] = []
    evidence: list[Evidence] = []

    if not r.is_pumpfun:
        flags.append(
            Flag(
                code="non_pumpfun",
                severity="info",
                detail="no pump.fun bonding-curve account for this mint; analysed best-effort",
            )
        )
    raw = RawFields(name=r.name, symbol=r.symbol)
    image = ImageInfo(status="missing")
    if m and m.status == "ok":
        raw.name = m.name or r.name
        raw.symbol = m.symbol or r.symbol
        raw.description = m.description
        raw.image_url = m.image_url
        raw.twitter = m.twitter
        raw.telegram = m.telegram
        raw.website = m.website
        if m.image_url:
            image = ImageInfo(
                status="ok" if m.image_content_key else "failed",
                source_url=m.image_url,
            )
            if not m.image_content_key and m.image_error:
                caveats.append(f"image could not be fetched: {m.image_error}")
        evidence.append(
            Evidence(
                kind="metadata",
                label="source",
                weight=0.0,
                detail=f"metadata fetched ({m.content_key})",
                source=f"uri:{r.uri}",
            )
        )
    else:
        reason = (m.error if m else "no uri on-chain") or "unknown"
        flags.append(
            Flag(
                code="metadata_unresolved",
                severity="info",
                detail=f"off-chain metadata unavailable ({reason}); using on-chain name/symbol",
            )
        )
        caveats.append("off-chain metadata (description, image, socials) was not available")
    if r.created_at is None:
        caveats.append("token creation time could not be determined")

    evidence.append(
        Evidence(
            kind="onchain",
            label="source",
            weight=0.0,
            detail=(
                f"{r.token_program} mint; pump.fun={r.is_pumpfun}; "
                f"on-chain metadata via {r.onchain_metadata_source}; "
                f"created_at via {r.created_at_source or 'unknown'}"
            ),
            source="solana-rpc",
        )
    )

    partial = (m is None or m.status != "ok") and bool(r.uri)
    summary = (
        f"{raw.name or '?'} (${raw.symbol or '?'}): on-chain and metadata resolved. "
        "Meaning analysis (engine stages) is not implemented yet in this build."
    )
    doc = Analysis(
        mint=r.mint,
        created_at=r.created_at,
        launchpad="pump.fun" if r.is_pumpfun else "unknown",
        market=Market(
            complete=r.complete,
            curve_progress=r.curve_progress,
            creator=r.creator,
            is_mayhem_mode=r.is_mayhem,
            quote_mint=r.quote_mint,
        ),
        raw=raw,
        image=image,
        x=_x_info(raw.twitter, r.created_at),
        flags=flags,
        summary=summary,
        evidence=evidence,
        caveats=caveats,
        depth=depth,  # type: ignore[arg-type]
        analyzed_at=now,
        versions=Versions(rules=RULES_VERSION, lexicon=LEXICON_VERSION),
    )
    if partial:
        doc.caveats.append("partial: metadata pending; the next request may be more complete")
    return doc


# ----------------------------------------------------------------- persistence


async def _store_analysis(conn: asyncpg.Connection, doc: Analysis) -> int:
    version = await conn.fetchval(
        """insert into analysis (mint, version, depth, doc, referent, categories, flags)
           values ($1, coalesce((select max(version) from analysis where mint=$1), 0) + 1,
                   $2, $3, $4, $5, $6)
           returning version""",
        doc.mint,
        doc.depth,
        doc.model_dump(mode="json"),
        doc.referent.label if doc.referent else None,
        [c.label for c in doc.categories],
        [f.code for f in doc.flags],
    )
    return int(version)


async def _store_xref(conn: asyncpg.Connection, mint: str, x: XInfo | None) -> None:
    if x is None:
        await conn.execute("delete from x_ref where mint=$1", mint)
        return
    await conn.execute(
        """insert into x_ref (mint, kind, tweet_id, community_id, handle, user_id, object_time)
           values ($1,$2,$3,$4,$5,$6,$7)
           on conflict (mint) do update set kind=excluded.kind, tweet_id=excluded.tweet_id,
             community_id=excluded.community_id, handle=excluded.handle,
             user_id=excluded.user_id, object_time=excluded.object_time""",
        mint,
        x.ref.kind,
        x.ref.tweet_id,
        x.ref.community_id,
        x.ref.handle or x.ref.url_handle,
        x.ref.user_id,
        x.object_time,
    )


async def _metadata_attempts(conn: asyncpg.Connection, mint: str) -> int:
    return await conn.fetchval("select attempts from token_metadata where mint=$1", mint) or 0


async def _schedule_retry(conn: asyncpg.Connection, mint: str, depth: str) -> None:
    from tokensage import queue

    row = await conn.fetchrow(
        "select next_retry_at from token_metadata where mint=$1 and status='unresolved'", mint
    )
    if not row or row["next_retry_at"] is None:
        return
    job = await conn.fetchrow(
        """insert into job (kind, mint, depth, priority, run_after)
           values ('retry_metadata', $1, $2, $3, $4)
           on conflict (kind, mint, depth) where status in ('pending','running') do nothing
           returning id""",
        mint,
        depth,
        queue.PRIORITY_BACKGROUND,
        row["next_retry_at"],
    )
    if job:
        log.info("metadata.retry_scheduled", mint=mint, run_after=str(row["next_retry_at"]))


# ----------------------------------------------------------------- entry points


async def analyze(conn: asyncpg.Connection, ctx: Context, mint: str, depth: str) -> int:
    """Resolve + fetch + build + persist. Returns the new analysis.version.
    Raises AnalyzeFailed for definitive per-CA failures (404/422 at the API)."""
    if ctx.rpc is None:
        raise RuntimeError("SOLANA_RPC_URL is not configured; the analyzer cannot resolve CAs")
    try:
        r = await resolve(conn, ctx.rpc, ctx.http, ctx.settings, mint)
    except ResolveError as e:
        raise AnalyzeFailed(e.code, str(e)) from e

    m: md.Metadata | None = None
    if r.uri:
        m = await md.load_cached(conn, r.mint)
        if m is None:
            attempts = await _metadata_attempts(conn, r.mint)
            m = await md.fetch_metadata(ctx.http, r.uri, ctx.settings)
            await md.persist(conn, r.mint, m, attempts + (1 if m.status != "ok" else 0))
            if m.status == "unresolved":
                await _schedule_retry(conn, r.mint, depth)
            log.info(
                "metadata.fetched",
                mint=r.mint,
                status=m.status,
                error=m.error,
                image=m.image_content_key,
            )

    doc = build_document(r, m, depth)
    await _store_xref(conn, r.mint, doc.x)
    return await _store_analysis(conn, doc)


async def retry_metadata(
    conn: asyncpg.Connection, ctx: Context, mint: str, depth: str
) -> int | None:
    """Background job: try the metadata again; on success re-run the analysis."""
    row = await conn.fetchrow("select uri from token where mint=$1", mint)
    if not row or not row["uri"]:
        return None
    attempts = await _metadata_attempts(conn, mint)
    m = await md.fetch_metadata(ctx.http, row["uri"], ctx.settings)
    await md.persist(conn, mint, m, attempts + (1 if m.status != "ok" else 0))
    if m.status == "ok":
        return await analyze(conn, ctx, mint, depth)
    if m.status == "unresolved":
        await _schedule_retry(conn, mint, depth)
    return None
