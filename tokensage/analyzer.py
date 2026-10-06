"""The analyzer entry point the worker calls per job.

Phase 3: resolve the CA on-chain, fetch metadata + image, gather database context (same-name
tokens, X link reuse, creator history, image-hash candidates), run the basic-depth engine
(guide §5), and persist a full Analysis. `full`-depth extras (OCR, X fetch, trends) are Phase 4.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage import fulldepth
from tokensage.api.schemas import (
    Analysis,
    Category,
    CopyOf,
    Evidence,
    Flag,
    ImageInfo,
    Market,
    NearDuplicate,
    RawFields,
    Referent,
    ReferentKind,
    Severity,
    Versions,
    XAuthor,
    XInfo,
    XQuoted,
    XRef,
)
from tokensage.api.schemas import (
    Normalized as NormalizedOut,
)
from tokensage.api.schemas import (
    Trend as TrendOut,
)
from tokensage.api.schemas import (
    TrendTerm as TrendTermOut,
)
from tokensage.config import Settings
from tokensage.engine import image as image_stage
from tokensage.engine import xsignals
from tokensage.engine.knowledge import KnownCoin, load_knowledge
from tokensage.engine.pipeline import (
    RULES_VERSION,
    DbContext,
    EngineInput,
    EngineOutput,
    SameNameToken,
    run_basic,
    run_full,
)
from tokensage.engine.xref import parse_x_ref, snowflake_time
from tokensage.resolve import metadata as md
from tokensage.resolve.resolver import Resolved, ResolveError, resolve
from tokensage.resolve.rpc import SolanaRpc
from tokensage.sources import lookups

log = structlog.get_logger("analyzer")

_REFERENT_KINDS: dict[str, ReferentKind] = {
    "famous_animal": "famous_animal",
    "meme": "meme",
    "person": "person",
    "coin": "coin",
    "event": "event",
    "concept": "concept",
    "place": "place",
    "other": "other",
}
_SEVERITIES: dict[str, Severity] = {"info": "info", "warn": "warn", "high": "high"}

LEXICON_VERSION = load_knowledge().versions.get("lexicon", "unknown")
__all__ = ["RULES_VERSION", "LEXICON_VERSION", "AnalyzeFailed", "Context", "analyze"]


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


# ----------------------------------------------------------------- X link (free part)


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


# ----------------------------------------------------------------- database context


async def _db_context(
    conn: asyncpg.Connection,
    ctx: Context,
    r: Resolved,
    m: md.Metadata | None,
    x: XInfo | None,
    ticker: str | None,
    name_compact: str,
) -> DbContext:
    dbc = DbContext()
    # X link reuse across other tokens
    if x is not None and x.ref.kind in ("tweet", "profile", "community"):
        if x.ref.tweet_id:
            dbc.x_reuse_count = await conn.fetchval(
                "select count(*) from x_ref where tweet_id=$1 and mint<>$2", x.ref.tweet_id, r.mint
            )
        elif x.ref.community_id:
            dbc.x_reuse_count = await conn.fetchval(
                "select count(*) from x_ref where community_id=$1 and mint<>$2",
                x.ref.community_id,
                r.mint,
            )
        elif x.ref.handle:
            dbc.x_reuse_count = await conn.fetchval(
                "select count(*) from x_ref where lower(handle)=lower($1) and mint<>$2",
                x.ref.handle,
                r.mint,
            )
    # creator history
    if r.creator:
        dbc.creator_token_count = await conn.fetchval(
            "select count(*) from token where creator=$1 and mint<>$2", r.creator, r.mint
        )
    # same-name tokens we already know, plus best-effort external searches
    if ticker or name_compact:
        rows = await conn.fetch(
            """select mint, name, symbol, created_at from token
               where mint<>$1 and (upper(symbol)=$2 or regexp_replace(lower(coalesce(name,'')),
                     '[^a-z0-9]', '', 'g') = $3)
               order by created_at nulls last limit 50""",
            r.mint,
            (ticker or "").upper(),
            name_compact,
        )
        dbc.same_name = [
            SameNameToken(x_["mint"], x_["name"], x_["symbol"], x_["created_at"], "db")
            for x_ in rows
        ]
        term = ticker or name_compact
        try:
            ext = await asyncio.gather(
                lookups.pumpfun_search(ctx.http, term),
                lookups.dexscreener_search(ctx.http, term),
            )
            found = lookups.filter_same_name([*ext[0], *ext[1]], ticker, name_compact)
            known = {t.mint for t in dbc.same_name} | {r.mint}
            dbc.same_name += [t for t in found if t.mint not in known]
        except Exception as e:  # noqa: BLE001 - lookups are optional
            log.info("lookups.failed", error=str(e)[:120])
    # known coins from the database (seed lives in data/, cron adds more)
    rows = await conn.fetch(
        """select id, symbol, name, aliases, lore, categories, mint, logo_phash, source
           from known_coin"""
    )
    for row in rows:
        dbc.extra_coins.append(
            KnownCoin(
                symbol=row["symbol"],
                name=row["name"],
                aliases=tuple(row["aliases"] or []),
                chain="solana",
                lore=row["lore"] or "",
                categories=tuple(row["categories"] or []),
                referent_label=row["name"],
                referent_kind="coin",
                referent_desc=row["lore"] or "",
                source=row["source"] or "db",
                mint=row["mint"],
                logo_phash=row["logo_phash"],
            )
        )
    # image hash candidates: known-coin logos + recent token images
    for c in dbc.extra_coins:
        if c.logo_phash is not None:
            dbc.image_candidates.append(
                image_stage.Candidate(f"known:{c.symbol}", c.logo_phash, known_coin=c.symbol)
            )
    this_key = m.image_content_key if m else None
    rows = await conn.fetch(
        """select i.content_key, i.phash, tm.mint from image i
           join token_metadata tm on tm.image_content_key = i.content_key
           where i.phash is not null and i.content_key <> coalesce($1, '')
           order by i.analyzed_at desc nulls last limit 20000""",
        this_key,
    )
    for row in rows:
        dbc.image_candidates.append(
            image_stage.Candidate(row["content_key"], row["phash"], mint=row["mint"])
        )
    return dbc


async def _persist_image(conn: asyncpg.Connection, key: str, out: EngineOutput) -> None:
    f = out.image.features
    if not f:
        return
    await conn.execute(
        """update image set phash=$2, dhash=$3, phash_mirror=$4, palette=$5, width=$6,
             height=$7, animated=$8, analyzed_at=now() where content_key=$1""",
        key,
        f.phash,
        f.dhash,
        f.phash_mirror,
        f.palette_hex,
        f.width,
        f.height,
        f.animated,
    )


async def _cached_image_features(
    conn: asyncpg.Connection, key: str
) -> image_stage.ImageFeatures | None:
    row = await conn.fetchrow("select * from image where content_key=$1 and phash is not null", key)
    if not row:
        return None
    return image_stage.ImageFeatures(
        phash=row["phash"],
        dhash=row["dhash"] or 0,
        phash_mirror=row["phash_mirror"] or row["phash"],
        width=row["width"] or 0,
        height=row["height"] or 0,
        animated=bool(row["animated"]),
        frames=1,
        palette_hex=list(row["palette"] or []),
        palette_names=[],
        format=None,
    )


# ----------------------------------------------------------------- document


def _quoted_out(q: xsignals.QuotedAssessment, token_created: datetime | None) -> XQuoted:
    status = q.status if q.status in ("ok", "deleted") else "failed"
    author = None
    if q.author_handle or q.followers is not None:
        author = XAuthor(
            handle=q.author_handle,
            user_id=q.author_id,
            name=q.author_name,
            verified_type=q.verified_type,
            followers=q.followers,
            joined=q.joined,
        )
    predates = None
    if token_created and q.created_at:
        predates = int((token_created - q.created_at).total_seconds())
    handle = q.author_handle or "i"
    return XQuoted(
        id=q.id,
        url=f"https://x.com/{handle}/status/{q.id}" if q.id else None,
        status=status,  # type: ignore[arg-type]
        author=author,
        text=q.text[:1000] if q.text else None,
        created_at=q.created_at,
        predates_token_by_s=predates,
    )


def build_document(
    r: Resolved,
    m: md.Metadata | None,
    depth: str,
    out: EngineOutput | None,
    x: XInfo | None,
    hint_use: HintUse | None = None,
) -> Analysis:
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
    meta_ok = bool(m and m.status == "ok")
    if m and meta_ok:
        raw.name = m.name or r.name
        raw.symbol = m.symbol or r.symbol
        raw.description = m.description
        raw.image_url = m.image_url
        raw.twitter = m.twitter
        raw.telegram = m.telegram
        raw.website = m.website
        if m.image_url:
            image = ImageInfo(
                status="ok" if m.image_content_key else "failed", source_url=m.image_url
            )
            if not m.image_content_key and m.image_error:
                caveats.append(f"image could not be fetched: {m.image_error}")
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

    if hint_use is None or hint_use.onchain_visible:
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
    if hint_use is not None:
        used = ", ".join(hint_use.fields) or "none"
        evidence.append(
            Evidence(
                kind="provenance",
                label="hints",
                weight=0.0,
                detail=(
                    f"caller-supplied hints used for: {used}"
                    + ("" if hint_use.onchain_visible else "; mint not yet visible on-chain")
                ),
                source="hints:caller",
            )
        )
        if m is not None and m.origin == "hints":
            caveats.append("hints: metadata supplied by caller")
        elif "created_at" in hint_use.fields:
            caveats.append("hints: creation time supplied by caller")
        if not hint_use.onchain_visible:
            caveats.append(
                "partial: mint not yet visible on-chain; analysed from caller hints "
                "(market data missing)"
            )
        caveats.extend(hint_use.notes)

    referent = None
    categories: list[Category] = []
    copy_of: list[CopyOf] = []
    ticker_explanation = None
    doc_trend = TrendOut()
    summary = f"{raw.name or '?'} (${raw.symbol or '?'}): resolved, but the engine did not run."
    if out is not None:
        agg = out.agg
        if agg.referent:
            referent = Referent(
                label=agg.referent.label,
                kind=_REFERENT_KINDS.get(agg.referent.kind, "other"),
                desc=agg.referent.desc,
                source=agg.referent.source,
                confidence=agg.referent.score,
            )
        categories = [Category(label=lbl, confidence=s) for lbl, s in agg.categories]
        copy_of = [
            CopyOf(
                ticker=c.get("ticker"), name=c.get("name"), mint=c.get("mint"), signals=c["signals"]
            )
            for c in out.copy_of
        ]
        ticker_explanation = out.ticker_explanation
        for ev in out.evidence:
            evidence.append(
                Evidence(
                    kind=ev.kind,
                    label=ev.label,
                    weight=round(ev.weight, 3),
                    detail=ev.detail,
                    source=ev.source,
                    url=ev.url,
                )
            )
        for f in out.flags:
            flags.append(
                Flag(code=f.code, severity=_SEVERITIES.get(f.severity, "info"), detail=f.detail)
            )
        caveats.extend(out.caveats)
        summary = out.summary
        if out.ocr_lines:
            image.ocr = [ln.text for ln in out.ocr_lines]
        if out.x is not None and x is not None:
            xa = out.x
            x.status = xa.status  # type: ignore[assignment]
            x.relation = xa.relation  # type: ignore[assignment]
            x.fetch_source = xa.fetch_source
            x.text = xa.text[:1000] if xa.text else None
            if xa.author_handle or xa.followers is not None:
                x.author = XAuthor(
                    handle=xa.author_handle,
                    user_id=xa.author_id,
                    name=xa.author_name,
                    verified_type=xa.verified_type,
                    followers=xa.followers,
                    joined=xa.joined,
                    username_changes=xa.username_changes,
                )
            if xa.quoted is not None:
                x.quoted = _quoted_out(xa.quoted, r.created_at)
        if out.trend_hits:
            doc_trend = TrendOut(
                matched=True,
                terms=[
                    TrendTermOut(term=h.term.term, spike=h.term.spike, source=h.term.source)
                    for h in out.trend_hits
                ],
            )
        else:
            doc_trend = TrendOut()
        feats = out.image.features
        if feats:
            image.phash = f"{feats.phash & ((1 << 64) - 1):016x}"
            image.palette = feats.palette_hex
            image.animated = feats.animated
            image.near_duplicates = [
                NearDuplicate(
                    mint=nd.candidate.mint,
                    known_coin=nd.candidate.known_coin,
                    template=nd.candidate.template,
                    distance=nd.distance,
                )
                for nd in out.image.near
            ]
            image.status = "ok"
        elif out.image.error and image.status == "ok":
            image.status = "failed"

    partial = (m is None or m.status != "ok") and bool(r.uri)
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
        normalized=_normalized_view(out),
        referent=referent,
        categories=categories,
        ticker_explanation=ticker_explanation,
        copy_of=copy_of,
        image=image,
        x=x,
        trend=doc_trend,
        flags=flags,
        summary=summary,
        evidence=evidence,
        caveats=list(dict.fromkeys(caveats)),
        depth=depth,  # type: ignore[arg-type]
        analyzed_at=now,
        versions=Versions(
            rules=RULES_VERSION,
            lexicon=LEXICON_VERSION,
            known_coins=load_knowledge().versions.get("known_coins"),
        ),
    )
    if partial:
        doc.caveats.append("partial: metadata pending; the next request may be more complete")
    return doc


def _normalized_view(out: EngineOutput | None) -> NormalizedOut:
    if out is None:
        return NormalizedOut()
    n = out.normalized
    return NormalizedOut(
        name_tokens=n.name_tokens,
        ticker=n.ticker or None,
        ticker_base=n.ticker_base or None,
        markers=[m.code for m in n.markers],
        emoji_keywords=n.emoji_keywords,
        obfuscation=n.obfuscation,
    )


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


@dataclass
class HintUse:
    """How caller-supplied hints were used for this analysis (surfaced in caveats/evidence)."""

    fields: list[str]
    onchain_visible: bool = True
    notes: list[str] = field(default_factory=list)


def _hint_created_at(hints: dict[str, Any] | None) -> datetime | None:
    raw = (hints or {}).get("created_at")
    if not raw:
        return None
    try:
        dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    # pump.fun launched in 2024; anything else (or from the future) is not a creation time
    if dt < datetime(2023, 1, 1, tzinfo=UTC) or dt > datetime.now(UTC) + timedelta(minutes=5):
        return None
    return dt


def _resolved_from_hints(mint: str, hints: dict[str, Any]) -> Resolved:
    """The mint is not visible on-chain yet (seconds-old coin): analyse from the hints."""
    return Resolved(
        mint=mint,
        token_program="unknown",
        is_pumpfun=mint.endswith("pump"),
        name=md._clean_str(hints.get("name"), 256),
        symbol=md._clean_str(hints.get("symbol"), 64),
        uri=None,
        creator=None,
        bonding_curve=None,
        complete=None,
        curve_progress=None,
        is_mayhem=None,
        quote_mint=None,
        created_at=_hint_created_at(hints),
        created_at_source="hints" if _hint_created_at(hints) else None,
        onchain_metadata_source="none",
    )


async def _persist_stub_token(conn: asyncpg.Connection, r: Resolved) -> None:
    """analysis rows reference token; keep only what is known so a later on-chain resolve
    fills everything else (persist() only overwrites nulls)."""
    await conn.execute(
        """insert into token (mint, created_at, created_at_source, seen_by)
           values ($1, $2, $3, '{request}') on conflict (mint) do nothing""",
        r.mint,
        r.created_at,
        r.created_at_source,
    )


async def analyze(
    conn: asyncpg.Connection,
    ctx: Context,
    mint: str,
    depth: str,
    hints: dict[str, Any] | None = None,
) -> int:
    """Resolve + fetch + engine + persist. Returns the new analysis.version.
    Raises AnalyzeFailed for definitive per-CA failures (404/422 at the API).

    hints: metadata the caller already has (name, symbol, description, image_url, twitter,
    telegram, website, created_at). They replace the metadata fetch and, for a mint not yet
    visible on-chain, the on-chain read; they never turn into a 404."""
    if ctx.rpc is None:
        raise RuntimeError("SOLANA_RPC_URL is not configured; the analyzer cannot resolve CAs")
    hints = {k: v for k, v in (hints or {}).items() if v not in (None, "")}
    hint_meta_fields = [k for k in md.HINT_FIELDS if k in hints]
    hint_use: HintUse | None = (
        HintUse(fields=[*hint_meta_fields, *(["created_at"] if "created_at" in hints else [])])
        if hints
        else None
    )
    try:
        r = await resolve(
            conn, ctx.rpc, ctx.http, ctx.settings, mint, created_hint=_hint_created_at(hints)
        )
    except ResolveError as e:
        if not (hint_meta_fields and e.code == "token_not_found"):
            raise AnalyzeFailed(e.code, str(e)) from e
        assert hint_use is not None
        r = _resolved_from_hints(mint, hints)
        hint_use.onchain_visible = False
        await _persist_stub_token(conn, r)
        log.info("analyze.from_hints", mint=mint, reason=e.code)

    m: md.Metadata | None = None
    image_bytes: bytes | None = None
    cached_feats: image_stage.ImageFeatures | None = None
    cached = await md.load_cached(conn, r.mint) if r.uri else None
    if cached is None and hint_meta_fields:
        # The caller already has the metadata: skip the IPFS round trip, still fetch the
        # image (guarded) so the logo can be hashed and compared.
        m = md.from_hints(hints)
        await md.attach_image(ctx.http, m, ctx.settings)
        image_bytes = m.image_bytes
        if m.image_content_key:
            cached_feats = await _cached_image_features(conn, m.image_content_key)
        assert hint_use is not None
        for f, chain_v in (("name", r.name), ("symbol", r.symbol)):
            hv = getattr(m, f)
            if chain_v and hv and hv.strip().casefold() != chain_v.strip().casefold():
                hint_use.notes.append(
                    f"hints: caller-supplied {f} '{hv[:40]}' differs from on-chain "
                    f"'{chain_v[:40]}'; using on-chain"
                )
                setattr(m, f, chain_v)
    elif r.uri:
        m = cached
        if m is None:
            attempts = await _metadata_attempts(conn, r.mint)
            m = await md.fetch_metadata(ctx.http, r.uri, ctx.settings)
            await md.persist(conn, r.mint, m, attempts + (1 if m.status != "ok" else 0))
            if m.status == "unresolved":
                await _schedule_retry(conn, r.mint, depth)
            log.info("metadata.fetched", mint=r.mint, status=m.status, error=m.error)
            image_bytes = m.image_bytes
        elif m.image_content_key:
            cached_feats = await _cached_image_features(conn, m.image_content_key)
            if cached_feats is None and m.image_url:
                # older row without hashes: fetch the image again (cached by CID upstream)
                refetched = await md.fetch_metadata(ctx.http, r.uri, ctx.settings)
                image_bytes = refetched.image_bytes

    name = (m.name if m and m.status == "ok" and m.name else None) or r.name
    symbol = (m.symbol if m and m.status == "ok" and m.symbol else None) or r.symbol
    desc = m.description if m and m.status == "ok" else None
    x = _x_info(m.twitter if m and m.status == "ok" else None, r.created_at)

    from tokensage.engine.normalize import clean_ticker, normalize

    n0 = normalize(name, symbol, None)
    dbc = await _db_context(conn, ctx, r, m, x, clean_ticker(symbol or ""), n0.name_compact)
    inp = EngineInput(
        mint=r.mint,
        name=name,
        symbol=symbol,
        description=desc,
        image_bytes=image_bytes,
        created_at=r.created_at,
        x_kind=x.ref.kind if x else None,
        x_object_time=x.object_time if x else None,
        ctx=dbc,
    )
    if depth == "full":
        tweet, profile = await fulldepth.x_content(conn, ctx.http, ctx.settings, x)
        inp.x_url_handle = x.ref.url_handle if x else None
        inp.tweet, inp.profile = tweet, profile
        inp.ocr_lines = await fulldepth.ocr_cached(conn, m.image_content_key if m else None)
        inp.run_ocr = inp.ocr_lines is None and image_bytes is not None
        inp.trend_index = await fulldepth.trend_index(conn)
        # CPU-bound (normalisation, image hashing, OCR): keep it off the event loop so the
        # worker's other concurrent jobs keep making network progress meanwhile.
        out = await asyncio.to_thread(run_full, inp)
        if inp.run_ocr and m and m.image_content_key and not out.ocr_error:
            await fulldepth.persist_ocr(conn, m.image_content_key, out.ocr_lines)
        await _attach_news(conn, ctx, out)
    else:
        out = await asyncio.to_thread(run_basic, inp)
    if x is not None:
        x.reuse_count = dbc.x_reuse_count
    if cached_feats is not None and out.image.features is None:
        out.image = image_stage.ImageResult(
            features=cached_feats,
            near=image_stage.near_duplicates(
                cached_feats,
                dbc.image_candidates,
                int(load_knowledge().scoring.get("logo_phash_edited", 14)),
            ),
        )
    if m and m.image_content_key and out.image.features and image_bytes:
        await _persist_image(conn, m.image_content_key, out)

    doc = build_document(r, m, depth, out, x, hint_use)
    await _store_xref(conn, r.mint, doc.x)
    return await _store_analysis(conn, doc)


async def _attach_news(conn: asyncpg.Connection, ctx: Context, out: EngineOutput) -> None:
    """Confirm the top trend hits against Google News and surface headline counts."""
    hits = sorted(out.trend_hits, key=lambda h: -h.term.spike)[: fulldepth.MAX_NEWS_LOOKUPS]
    for h in hits:
        heads = await fulldepth.news_for(conn, ctx.http, h.term.term)
        if heads is None:
            continue
        if heads:
            out.caveats.append(
                f"news check: {len(heads)} recent headline(s) for '{h.term.term}', e.g. "
                f'"{heads[0]["title"][:90]}"'
            )
        else:
            out.caveats.append(f"news check: no recent headlines found for '{h.term.term}'")


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
