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

from tokensage import fulldepth, gazetteer_db, queue, recall
from tokensage.api.schemas import (
    Analysis,
    Category,
    CopyOf,
    Evidence,
    Flag,
    ImageInfo,
    ImageLabel,
    Lineage,
    Market,
    NearDuplicate,
    OriginalMarket,
    Pair,
    PairReferent,
    RawFields,
    Referent,
    ReferentKind,
    ReferentWave,
    Severity,
    Versions,
    XAccount,
    XAuthor,
    XInfo,
    XLinkAccount,
    XMatchField,
    XMatchImage,
    XMatchReferent,
    XQuoted,
    XRef,
)
from tokensage.api.schemas import (
    CreatorFee as CreatorFeeOut,
)
from tokensage.api.schemas import (
    Normalized as NormalizedOut,
)
from tokensage.api.schemas import (
    Trend as TrendOut,
)
from tokensage.api.schemas import (
    TrendSource as TrendSourceOut,
)
from tokensage.api.schemas import (
    TrendTerm as TrendTermOut,
)
from tokensage.api.schemas import (
    XMatch as XMatchOut,
)
from tokensage.config import Settings
from tokensage.engine import (
    embed,
    meta,
    ocr,
    pairing,
    trends,
    vision,
    wikilookup,
    xmatch,
    xsignals,
)
from tokensage.engine import image as image_stage
from tokensage.engine import lineage as lineage_stage
from tokensage.engine.context import ReferentCandidate
from tokensage.engine.knowledge import KnownCoin, load_knowledge
from tokensage.engine.meta import MetaCounts, MetaWord, TopVolume
from tokensage.engine.pipeline import (
    RULES_VERSION,
    DbContext,
    EngineInput,
    EngineOutput,
    PriorRead,
    SameNameToken,
    run_basic,
    run_full,
)
from tokensage.engine.xref import parse_x_ref, snowflake_time
from tokensage.net import metrics
from tokensage.resolve import metadata as md
from tokensage.resolve import pair as pair_lookup
from tokensage.resolve.fees import creator_fee_to_dict
from tokensage.resolve.resolver import Resolved, ResolveError, curves_now, resolve
from tokensage.resolve.rpc import SolanaRpc
from tokensage.sources import bluesky, gnews, lookups

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
    "animal": "animal",
    "media": "media",
    "project": "project",
    "object": "object",
    "organization": "organization",
}
_SEVERITIES: dict[str, Severity] = {"info": "info", "warn": "warn", "high": "high"}

LEXICON_VERSION = load_knowledge().versions.get("lexicon", "unknown")
__all__ = [
    "RULES_VERSION",
    "LEXICON_VERSION",
    "AnalyzeFailed",
    "Context",
    "RetryRescheduled",
    "analyze",
]


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
            # the transport meters every upstream call for the admin panel; the pool limits
            # live on it, since httpx ignores `limits` when a transport is passed. Sized for
            # 8 slots that each hedge up to 4 IPFS gateways (or fetch 4 X images) plus a trend
            # poll: with 20, hung gateways queued the hedges behind the pool and doubled fetch
            # latency. Idle connections live 30 s, not httpx's 5 s: at ~700 analyses/h the gap
            # between calls to one host averages ~5 s, so a third of calls paid a new handshake
            transport=metrics.MeteredTransport(
                limits=httpx.Limits(
                    max_connections=64, max_keepalive_connections=32, keepalive_expiry=30.0
                )
            ),
        )
        metrics.meter.configure(
            rpc_url=settings.solana_rpc_url,
            ipfs_gateways=settings.ipfs_gateway_list,
            costs=settings.helius_credit_costs,
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


async def _x_reuse(
    conn: asyncpg.Connection, x: XInfo, mint: str, created_at: datetime | None
) -> tuple[int, int | None, datetime | None]:
    """Other analysed tokens linking the same post/profile/community: how many, this coin's
    place among them all by launch time (1 = the first), and when the first one launched.
    Rank and first launch are None when this coin's own launch time is unknown."""
    if x.ref.kind not in ("tweet", "profile", "community"):
        return 0, None, None
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if x.ref.tweet_id:
        where, key = "x.tweet_id=$1", x.ref.tweet_id
    elif x.ref.community_id:
        where, key = "x.community_id=$1", x.ref.community_id
    elif x.ref.handle:
        where, key = "lower(x.handle)=lower($1)", x.ref.handle
    else:
        return 0, None, None
    row = await conn.fetchrow(
        f"""select count(*) as n,
                  count(*) filter (where t.created_at < $3) as earlier,
                  min(t.created_at) as first_at
           from x_ref x left join token t on t.mint = x.mint
           where {where} and x.mint<>$2""",
        key,
        mint,
        created_at,
    )
    n = int(row["n"]) if row else 0
    if created_at is None or row is None:
        return n, None, None
    first = row["first_at"]
    return n, int(row["earlier"]) + 1, min(first, created_at) if first is not None else created_at


# ----------------------------------------------------------------- database context


async def _external_namesakes(
    http: httpx.AsyncClient, ticker: str | None, name_compact: str
) -> list[SameNameToken]:
    """Best-effort same-name coins from pump.fun and DexScreener search; never raises."""
    try:
        ext = await asyncio.gather(
            lookups.pumpfun_search(http, ticker or name_compact),
            lookups.dexscreener_search(http, ticker or name_compact),
        )
        return lookups.filter_same_name([*ext[0], *ext[1]], ticker, name_compact)
    except Exception as e:  # noqa: BLE001 - lookups are optional
        log.info("lookups.failed", error=str(e)[:120])
        return []


async def _db_context(
    conn: asyncpg.Connection,
    ctx: Context,
    r: Resolved,
    m: md.Metadata | None,
    x: XInfo | None,
    ticker: str | None,
    name_compact: str,
    meta_words: list[str] | None = None,
    logo: image_stage.ImageFeatures | None = None,
) -> DbContext:
    # The external searches only need the name and ticker: they run while the database
    # queries below do, instead of adding their round trip after them.
    ext_task = (
        asyncio.create_task(_external_namesakes(ctx.http, ticker, name_compact))
        if ticker or name_compact
        else None
    )
    try:
        return await _db_context_queries(
            conn, ctx, r, x, ticker, name_compact, meta_words, logo, ext_task
        )
    finally:
        if ext_task is not None and not ext_task.done():
            ext_task.cancel()


async def _db_context_queries(
    conn: asyncpg.Connection,
    ctx: Context,
    r: Resolved,
    x: XInfo | None,
    ticker: str | None,
    name_compact: str,
    meta_words: list[str] | None,
    logo: image_stage.ImageFeatures | None,
    ext_task: asyncio.Task[list[SameNameToken]] | None,
) -> DbContext:
    dbc = DbContext()
    if x is not None:
        dbc.x_reuse_count, dbc.x_reuse_rank, dbc.x_reuse_first_at = await _x_reuse(
            conn, x, r.mint, r.created_at
        )
    # creator history
    if r.creator:
        dbc.creator_token_count = await conn.fetchval(
            "select count(*) from token where creator=$1 and mint<>$2", r.creator, r.mint
        )
    # same-name tokens we already know, plus best-effort external searches
    dbc.copycat_window_days = ctx.settings.copycat_window_days
    meta_cfg = {
        "window_hours": int(load_knowledge().meta.get("window_hours", 24)),
        "history_days": int(load_knowledge().meta.get("history_days", 90)),
    }
    if ticker or name_compact:
        # an empty name or ticker must not match every token whose name/ticker is empty
        # the earliest namesakes launched within the copycat window before this one: the
        # candidates for the coin it copies (an older namesake is never its original)
        rows = await conn.fetch(
            """select mint, name, symbol, created_at from token
               where mint<>$1 and (($2 <> '' and upper(symbol)=$2) or ($3 <> '' and
                     regexp_replace(lower(coalesce(name,'')), '[^a-z0-9]', '', 'g') = $3))
                 and created_at >= coalesce($4, now()) - make_interval(days => $5)
                 and created_at <= coalesce($4, now())
               order by created_at limit 50""",
            r.mint,
            (ticker or "").upper(),
            name_compact,
            r.created_at,
            ctx.settings.copycat_window_days,
        )
        # ... and every namesake launched around this one, for its copycat rank and the
        # current-meta count (the query above keeps only the oldest 50)
        rows = [
            *rows,
            *await conn.fetch(
                """select mint, name, symbol, created_at from token
                   where mint<>$1 and (($2 <> '' and upper(symbol)=$2) or ($3 <> '' and
                         regexp_replace(lower(coalesce(name,'')), '[^a-z0-9]', '', 'g') = $3))
                     and created_at >= coalesce($4, now()) - make_interval(hours => $5)
                     and created_at <= coalesce($4, now()) + make_interval(hours => $5)
                   order by created_at limit 2000""",
                r.mint,
                (ticker or "").upper(),
                name_compact,
                r.created_at,
                meta_cfg["window_hours"],
            ),
        ]
        dbc.same_name = list(
            {
                x_["mint"]: SameNameToken(
                    x_["mint"], x_["name"], x_["symbol"], x_["created_at"], "db"
                )
                for x_ in rows
            }.values()
        )
    if meta_words:
        dbc.meta_counts = await _meta_counts(conn, r, meta_words, **meta_cfg)
    # the most-traded tokens of the snapshot nearest this launch (a day either side)
    rows = await conn.fetch(
        """select rank, mint, name, symbol, volume_usd from top_volume
           where day = (select max(day) from top_volume
                        where day between coalesce($1, now())::date - 1
                                      and coalesce($1, now())::date + 1)
           order by rank""",
        r.created_at,
    )
    dbc.top_volume = [
        TopVolume(
            row["rank"],
            row["mint"],
            row["name"] or "",
            row["symbol"] or "",
            row["volume_usd"] or 0.0,
        )
        for row in rows
    ]
    dbc.gazetteer = await gazetteer_db.current(conn)
    # known coins from the database (seed lives in data/, cron adds more), and their logos
    # as image hash candidates (recent token images are added below)
    coins, logos = await _known_coins(conn)
    dbc.extra_coins = list(coins)
    dbc.image_candidates.extend(logos)
    # Logos of tokens launched before this one (within the logo scan window) that are
    # near-duplicates of its own, plain or mirrored. Postgres does the Hamming filter, so
    # only matches come back, however many coins launched. The same image file (same
    # content key) is the plainest logo reuse of all and counts too.
    if logo is not None:
        k = load_knowledge()
        scan_days = min(
            ctx.settings.copycat_window_days,
            int(lineage_stage.config(k).get("logo_scan_days", 7)),
        )
        window = """t.mint <> $1
             and t.created_at >= coalesce($2, now()) - make_interval(days => $3)
             and t.created_at <= coalesce($2, now())"""
        args = (
            r.mint,
            r.created_at,
            scan_days,
            logo.phash,
            logo.phash_mirror,
            int(k.scoring.get("logo_phash_edited", 14)),
        )
        # token.logo_phash (kept by triggers) narrows the window to the near matches from
        # one index (token_logo_idx) without visiting token_metadata and image per coin;
        # the join below re-checks each against the image's own hash
        near_sql = f"""select t.mint from token t
             where t.logo_phash is not null and {window}
               and least(bit_count((t.logo_phash # $4::bigint)::bit(64)),
                         bit_count((t.logo_phash # $5::bigint)::bit(64))) <= $6"""
        # as many as the join below keeps (the newest), plus the earliest: a logo reused by
        # most of the window must not send every one of its coins to the join
        near = [
            row["mint"]
            for row in await conn.fetch(
                f"{near_sql} order by t.created_at desc limit {LOGO_ROWS}", *args
            )
        ]
        if len(near) == LOGO_ROWS:
            seen = set(near)
            near += [
                row["mint"]
                for row in await conn.fetch(f"{near_sql} order by t.created_at limit 10", *args)
                if row["mint"] not in seen
            ]
        logo_sql = f"""from token t
           join token_metadata tm on tm.mint = t.mint
           join image i on i.content_key = tm.image_content_key
           where t.mint = any($7::text[]) and i.phash is not null and {window}
             and least(bit_count((i.phash # $4::bigint)::bit(64)),
                       bit_count((i.phash # $5::bigint)::bit(64))) <= $6"""
        rows = []
        if near:
            rows = await conn.fetch(
                f"""select i.content_key, i.phash, tm.mint, t.created_at, t.name, t.symbol
                    {logo_sql} order by t.created_at desc limit {LOGO_ROWS}""",
                *args,
                near,
            )
        if len(rows) == LOGO_ROWS:
            # a logo reused thousands of times: keep the recent ones (the counts) and add
            # the earliest (the original, logo_first_seen_at)
            rows = [
                *rows,
                *await conn.fetch(
                    f"""select i.content_key, i.phash, tm.mint, t.created_at, t.name, t.symbol
                        {logo_sql} order by t.created_at limit 10""",
                    *args,
                    near,
                ),
            ]
        for row in rows:
            dbc.image_candidates.append(
                image_stage.Candidate(
                    row["content_key"],
                    row["phash"],
                    mint=row["mint"],
                    created_at=row["created_at"],
                    name=row["name"],
                    symbol=row["symbol"],
                )
            )
    if ext_task is not None:
        # nothing above reads same_name: the external namesakes join it here, after ours
        found = await ext_task
        known = {t.mint for t in dbc.same_name} | {r.mint}
        dbc.same_name += [t for t in found if t.mint not in known]
    dbc.originals = await _prior_reads(
        conn,
        lineage_stage.candidate_mints(
            r.mint,
            r.created_at,
            dbc.same_name,
            [(c.mint, c.created_at) for c in dbc.image_candidates if c.mint],
            ctx.settings.copycat_window_days,
            top_mints=[
                t.mint
                for t in dbc.top_volume
                if (ticker and t.symbol.upper() == ticker.upper())
                or (name_compact and _compact(t.name) == name_compact)
            ],
        ),
    )
    return dbc


LOGO_ROWS = 5000


# The known_coin table, materialised once per change: the knowledge cron rewrites it about
# weekly, while every read used to fetch and rebuild all of it (~1,250 rows). A signature
# query (row count, newest update, xmin sum: any insert, update or delete changes it) runs
# on every read, so a change is seen by the next read, exactly as before.
_known_cache: tuple[tuple[Any, ...], list[KnownCoin], list[image_stage.Candidate]] | None = None


async def _known_coins(
    conn: asyncpg.Connection,
) -> tuple[list[KnownCoin], list[image_stage.Candidate]]:
    global _known_cache
    row = await conn.fetchrow(
        """select count(*) n, max(updated_at) u, sum(xmin::text::bigint) x from known_coin"""
    )
    sig = (row["n"], row["u"], row["x"])
    if _known_cache and _known_cache[0] == sig:
        return _known_cache[1], _known_cache[2]
    rows = await conn.fetch(
        """select id, symbol, name, aliases, lore, categories, mint, logo_phash, source
           from known_coin"""
    )
    coins = [
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
        for row in rows
    ]
    logos = [
        image_stage.Candidate(f"known:{c.symbol}", c.logo_phash, known_coin=c.symbol)
        for c in coins
        if c.logo_phash is not None
    ]
    _known_cache = (sig, coins, logos)
    return coins, logos


def _compact(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


async def _prior_reads(conn: asyncpg.Connection, mints: list[str]) -> dict[str, PriorRead]:
    """The latest stored read of each mint: what a copy of it inherits."""
    if not mints:
        return {}
    rows = await conn.fetch(
        """select distinct on (mint) mint, doc->'categories' as cats, doc->'referent' as ref
           from analysis where mint = any($1::text[]) order by mint, version desc""",
        mints,
    )
    out: dict[str, PriorRead] = {}
    for row in rows:
        cats = row["cats"] if isinstance(row["cats"], list) else []
        prior = PriorRead(
            categories=[
                (str(c["label"]), float(c["confidence"]))
                for c in cats
                if isinstance(c, dict) and "label" in c and "confidence" in c
            ]
        )
        ref = row["ref"]
        if isinstance(ref, dict) and ref.get("label"):
            prior.referent = ReferentCandidate(
                label=str(ref["label"]),
                kind=str(ref.get("kind") or "other"),
                desc=ref.get("desc"),
                source=str(ref.get("source") or "analysis"),
                score=float(ref.get("confidence") or 0.0),
                generic=bool(ref.get("generic")),
            )
        out[row["mint"]] = prior
    return out


async def _meta_counts(
    conn: asyncpg.Connection,
    r: Resolved,
    words: list[str],
    window_hours: int,
    history_days: int,
) -> MetaCounts:
    """How many token names carry each word within `window_hours` of this launch, and over
    the `history_days` before it, plus the totals: the current-meta signal's inputs."""
    bounds = """t.created_at > coalesce($1, now()) - make_interval(days => $3)
                and t.created_at <= coalesce($1, now()) + make_interval(hours => $2)"""
    recent = """t.created_at >= coalesce($1, now()) - make_interval(hours => $2)"""
    # the totals count every token of the window but this one: count the window without a
    # mint filter (an index-only scan of token_created_at_idx, no heap pages) and take this
    # token's own row off with a primary-key lookup
    tot = await conn.fetchrow(
        f"""select a.recent - s.recent as recent, a.total - s.total as total
            from (select count(*) filter (where {recent}) as recent, count(*) as total
                  from token t where {bounds}) a,
                 (select count(*) filter (where {recent}) as recent, count(*) as total
                  from token t where {bounds} and t.mint = $4) s""",
        r.created_at,
        window_hours,
        history_days,
        r.mint,
    )
    # the words are lowercase [a-z0-9]+ (meta.candidate_words), safe inside the regex
    rows = await conn.fetch(
        f"""select w, count(*) filter (where {recent}) as recent, count(*) as total
            from unnest($5::text[]) as w
            join token t on lower(coalesce(t.name, '')) ~ ('\\m' || w || '\\M')
            where {bounds} and t.mint <> $4
            group by w""",
        r.created_at,
        window_hours,
        history_days,
        r.mint,
        words,
    )
    return MetaCounts(
        recent_total=int(tot["recent"] or 0),
        history_total=int(tot["total"] or 0),
        words=[MetaWord(row["w"], int(row["recent"]), int(row["total"])) for row in rows],
    )


async def _vision_labels(
    conn: asyncpg.Connection, ctx: Context, m: md.Metadata | None, image_bytes: bytes | None
) -> vision.VisionResult | None:
    """The logo's labels (ENABLE_CLIP, full depth): cached per image and model, else run.
    A logo whose hashes and OCR came from cache has no bytes here; it is fetched once more
    so the labels can be computed and cached. None when the model or the logo is missing."""
    if m is None or not m.image_url:
        return None
    # first call loads the ONNX model from disk: keep that off the event loop
    enc = await asyncio.to_thread(vision.default_encoder, ctx.settings)
    if enc is None:
        return None
    key = m.image_content_key
    cached = await fulldepth.vision_cached(conn, key, vision.load_config().model)
    if cached is not None:
        return cached
    data = image_bytes
    if data is None:
        tmp = md.Metadata(status="ok", image_url=m.image_url)
        await md.attach_image(ctx.http, tmp, ctx.settings)
        data, key = tmp.image_bytes, tmp.image_content_key or key
    if not data:
        return None
    res = await vision.label_async(enc, data)
    if res.error is None and key:
        await fulldepth.persist_vision(conn, key, res)
    elif res.error:
        log.info("vision.failed", key=key, error=res.error)
    return res


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


def _match_out(m: xmatch.XMatch) -> XMatchOut:
    return XMatchOut(
        name=XMatchField(score=m.name.score, how=m.name.how, detail=m.name.detail),
        ticker=XMatchField(score=m.ticker.score, how=m.ticker.how, detail=m.ticker.detail),
        image=XMatchImage(
            score=m.image.score,
            best_distance=m.image.best_distance,
            media_checked=m.image.media_checked,
            detail=m.image.detail,
        ),
        referent=XMatchReferent(
            x_label=m.referent.x_label,
            x_kind=m.referent.x_kind,
            agrees=m.referent.agrees,
            confidence=m.referent.confidence,
        ),
        x_categories=[Category(label=lbl, confidence=c) for lbl, c in m.x_categories],
        fit=m.fit,
        verdict=m.verdict,  # type: ignore[arg-type]
        basis=m.basis,  # type: ignore[arg-type]
    )


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
    extras: ReadExtras | None = None,
) -> Analysis:
    now = datetime.now(UTC)
    ex = extras or ReadExtras()
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
    main_category: Category | None = None
    copy_of: list[CopyOf] = []
    lineage: Lineage | None = None
    ticker_explanation = None
    doc_trend = TrendOut()
    summary = f"{raw.name or '?'} (${raw.symbol or '?'}): resolved, but the engine did not run."
    if out is not None:
        agg = out.agg
        rr = out.referent_read
        if rr is not None:
            referent = Referent(
                label=rr.label,
                kind=_REFERENT_KINDS.get(rr.kind, "other"),
                desc=rr.desc,
                source=rr.source,
                confidence=rr.confidence,
                supported_by=rr.supported_by,
                generic=rr.generic,
                wave=None if rr.generic else ex.wave,
            )
        categories = [
            Category(
                label=lbl,
                confidence=s,
                inputs=agg.inputs.get(lbl),
                wave_1h=ex.category_waves.get(lbl),
            )
            for lbl, s in agg.categories
        ]
        main = agg.main
        main_category = next((c for c in categories if main and c.label == main[0]), None)
        copy_of = [
            CopyOf(
                ticker=c.get("ticker"),
                name=c.get("name"),
                mint=c.get("mint"),
                signals=c["signals"],
                created_at=c.get("created_at"),
                recent=c.get("recent"),
                rank=c.get("rank"),
                rank_of=c.get("rank_of"),
                rank_window_hours=c.get("rank_window_hours"),
                original_age_s=c.get("original_age_s"),
                original_market=ex.markets.get(c.get("mint") or "") if c.get("recent") else None,
                match=c.get("match") or [],
                image_distance=c.get("image_distance"),
            )
            for c in out.copy_of
        ]
        lineage = _lineage_out(out.lineage)
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
                    where=ev.where,
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
        if out.vision is not None and out.vision.top:
            image.labels = [
                ImageLabel(label=c, score=s, model=out.vision.model) for c, s in out.vision.top
            ]
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
            if xa.replied_to is not None:
                x.replied_to = _quoted_out(xa.replied_to, r.created_at)
            x.accounts = [
                XAccount(
                    role=acc.role,  # type: ignore[arg-type]
                    handle=acc.handle,
                    name=acc.name,
                    followers=acc.followers,
                    verified_type=acc.verified_type,
                )
                for acc in xa.accounts
            ]
        if out.x_match is not None and x is not None:
            x.match = _match_out(out.x_match)
        if x is not None and out.x_account is not None:
            acc = out.x_account
            x.account = XLinkAccount(
                handle=acc.handle,
                created_at=acc.created_at,
                age_at_launch_s=acc.age_at_launch_s,
                posts_total=acc.posts_total,
                posts_about_coin=acc.posts_about_coin,
                name_changes=acc.name_changes,
                verified_type=acc.verified_type,
                made_for_coin=acc.made_for_coin,
            )
        if x is not None:
            x.credibility = out.x_credibility
        if out.depth == "full":
            doc_trend = _trend_out(out)
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

    # where the creator fee goes (rules 0.19.0): a flag per redirect, a summary clause
    fee_out: CreatorFeeOut | None = None
    if r.creator_fee is not None:
        cf = r.creator_fee
        fee_out = CreatorFeeOut.model_validate(creator_fee_to_dict(cf))
        code = _FEE_FLAGS.get(cf.destination)
        if code:
            flags.append(Flag(code=code, severity="info", detail=cf.describe()))
        if cf.destination not in ("creator", "unknown"):
            sentence = cf.describe()
            summary = f"{summary.rstrip()} {sentence[0].upper()}{sentence[1:]}."
        caveats.extend(f"creator fee: {c}" for c in cf.caveats)

    # "partial" promises a better result later: only when the metadata may still resolve
    # (invalid metadata is final and is reported via the metadata_unresolved flag instead)
    partial = (m is None or m.status in ("unresolved", "pending")) and bool(r.uri)
    doc = Analysis(
        mint=r.mint,
        created_at=r.created_at,
        launchpad="pump.fun" if r.is_pumpfun else "unknown",
        market=Market(
            complete=r.complete,
            curve_progress=r.curve_progress,
            creator=r.creator,
            creator_onchain=r.creator_onchain,
            creator_kind=r.creator_kind,  # type: ignore[arg-type]
            creator_fee=fee_out,
            is_mayhem_mode=r.is_mayhem,
            quote_mint=r.quote_mint,
            pair=_pair_out(out.pair) if out is not None else None,
        ),
        raw=raw,
        normalized=_normalized_view(out),
        referent=referent,
        categories=categories,
        main_category=main_category,
        ticker_explanation=ticker_explanation,
        copy_of=copy_of,
        lineage=lineage,
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


_FEE_FLAGS = {
    "holder_rewards": "creator_fee_holders",
    "charity": "creator_fee_charity",
    "github": "creator_fee_github",
    "wallet": "creator_fee_wallet",
    "split": "creator_fee_split",
    "social": "creator_fee_social",
    "other": "creator_fee_other",
    "cashback": "creator_fee_cashback",
}


def _lineage_out(lin: lineage_stage.Lineage | None) -> Lineage | None:
    if lin is None:
        return None
    o = lin.original
    ref = lin.reference
    return Lineage(
        kind=lin.kind,  # type: ignore[arg-type]
        of_mint=o.mint if o else (ref.mint if ref else None),
        of_name=o.name if o else (ref.name if ref else None),
        of_ticker=o.ticker if o else (ref.symbol if ref else None),
        of_created_at=o.created_at if o else None,
        match=list(o.match) if o else [],  # type: ignore[arg-type]
        rank=lin.rank,
        rank_of=lin.rank_of,
        window_hours=lin.window_hours,
        siblings_1h=lin.siblings_1h,
        siblings_6h=lin.siblings_6h,
        siblings_24h=lin.siblings_24h,
        logo_reuse_24h=lin.logo_reuse_24h,
        logo_first_seen_at=lin.logo_first_seen_at,
    )


def _pair_out(p: pairing.PairAssessment | None) -> Pair | None:
    if p is None:
        return None
    ref = None
    if p.referent is not None:
        ref = PairReferent(
            label=p.referent.label,
            kind=_REFERENT_KINDS.get(p.referent.kind, "other"),
            desc=p.referent.desc,
            confidence=round(max(0.0, min(1.0, p.referent.score)), 3),
        )
    return Pair(
        mint=p.mint,
        symbol=p.symbol,
        name=p.name,
        kind=p.kind,  # type: ignore[arg-type]
        source=p.source,
        underlying=p.underlying,
        builds_on=p.builds_on,
        builds_on_detail=p.builds_on_detail,
        referent=ref,
        categories=[
            Category(label=lbl, confidence=round(max(0.0, min(1.0, c)), 3))
            for lbl, c in p.categories
        ],
    )


def _normalized_view(out: EngineOutput | None) -> NormalizedOut:
    if out is None:
        return NormalizedOut()
    n = out.normalized
    return NormalizedOut(
        name_tokens=n.name_tokens,
        ticker=n.ticker or None,
        ticker_base=n.ticker_base or None,
        markers=[m.code for m in n.markers],
        emoji_keywords=list(dict.fromkeys([*n.emoji_keywords, *n.desc_emoji_keywords])),
        obfuscation=n.obfuscation,
    )


# ----------------------------------------------------------------- persistence


async def _store_analysis(conn: asyncpg.Connection, doc: Analysis) -> int:
    async with conn.transaction():
        # basic and full jobs for one mint can finish together: serialise max(version)+1
        await conn.execute("select pg_advisory_xact_lock(hashtext('analysis:' || $1))", doc.mint)
        version = await _insert_analysis(conn, doc)
        await _store_read(conn, doc)
        return version


async def _store_read(conn: asyncpg.Connection, doc: Analysis) -> None:
    """The coin's latest read by launch time: what the referent and category waves count.
    A coin with no known launch time is not counted (it would look new on every re-read)."""
    if doc.created_at is None:
        return
    await conn.execute(
        """insert into token_read (mint, launched_at, referent_key, referent_label, categories,
                                   updated_at)
           values ($1, $2, $3, $4, $5, now())
           on conflict (mint) do update set launched_at=excluded.launched_at,
             referent_key=excluded.referent_key, referent_label=excluded.referent_label,
             categories=excluded.categories, updated_at=now()""",
        doc.mint,
        doc.created_at,
        # a generic referent ("frog") is a kind, not one idea coins pile onto
        referent_key(doc.referent.label) if doc.referent and not doc.referent.generic else None,
        doc.referent.label if doc.referent and not doc.referent.generic else None,
        [c.label for c in doc.categories],
    )


async def _insert_analysis(conn: asyncpg.Connection, doc: Analysis) -> int:
    version = await conn.fetchval(
        """insert into analysis (mint, version, depth, doc, referent, referent_score,
                                 categories, flags)
           values ($1, coalesce((select max(version) from analysis where mint=$1), 0) + 1,
                   $2, $3, $4, $5, $6, $7)
           returning version""",
        doc.mint,
        doc.depth,
        doc.model_dump(mode="json"),
        doc.referent.label if doc.referent else None,
        doc.referent.confidence if doc.referent else None,
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


async def _schedule_retry(
    conn: asyncpg.Connection, mint: str, depth: str, own_job_id: int | None = None
) -> bool:
    """Queue the next metadata retry at token_metadata.next_retry_at. Called from inside a
    retry_metadata job (own_job_id), the job re-queues itself: inserting a new row would hit
    the single-flight index on the very job that is running, and the chain would stop."""
    from tokensage import queue

    row = await conn.fetchrow(
        "select next_retry_at from token_metadata where mint=$1 and status='unresolved'", mint
    )
    if not row or row["next_retry_at"] is None:
        return False
    if own_job_id is not None:
        await conn.execute(
            """update job set status='pending', locked_until=null, run_after=$2,
                 attempts = greatest(attempts - 1, 0)
               where id=$1""",
            own_job_id,
            row["next_retry_at"],
        )
        log.info("metadata.retry_scheduled", mint=mint, run_after=str(row["next_retry_at"]))
        return True
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
    return job is not None


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
        # An IPFS logo already hashed (and, at full depth, already OCR'd) needs no download:
        # its content key comes from the CID, and the bytes would only be hashed again.
        ipfs_key = md.ipfs_content_key(m.image_url)
        if ipfs_key:
            cached_feats = await _cached_image_features(conn, ipfs_key)
            if cached_feats is not None and depth == "full":
                if await fulldepth.ocr_cached(conn, ipfs_key) is None:
                    cached_feats = None  # OCR has not run on it yet: it needs the bytes
        if cached_feats is not None:
            m.image_content_key = ipfs_key
        else:
            await md.attach_image(ctx.http, m, ctx.settings)
            image_bytes = m.image_bytes
            if m.image_content_key:
                # register the hinted logo so its hashes are cached and it can be matched
                await md.persist_image_row(conn, m)
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
        if m is not None and hint_use is not None:
            # stored (fetched) metadata wins over hints; only a creation-time hint still counts
            hint_use.fields = [f for f in hint_use.fields if f == "created_at"]
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
                if (
                    refetched.image_content_key
                    and refetched.image_content_key != m.image_content_key
                ):
                    # the logo changed: keep hashes under the new image's own key
                    await md.persist_image_row(conn, refetched)
                    await conn.execute(
                        "update token_metadata set image_content_key=$2 where mint=$1",
                        r.mint,
                        refetched.image_content_key,
                    )
                    m.image_content_key = refetched.image_content_key

    name = (m.name if m and m.status == "ok" and m.name else None) or r.name
    symbol = (m.symbol if m and m.status == "ok" and m.symbol else None) or r.symbol
    desc = m.description if m and m.status == "ok" else None
    x = _x_info(m.twitter if m and m.status == "ok" else None, r.created_at)

    from tokensage.engine.normalize import clean_ticker, normalize

    n0 = normalize(name, symbol, None)
    logo_feats = cached_feats
    if logo_feats is None and image_bytes:
        # hash the logo up front: the database looks up its near-duplicates (lineage), and
        # the engine reuses these hashes instead of decoding the image again
        logo_feats = (await asyncio.to_thread(image_stage.analyze, image_bytes, [], 0)).features
    dbc = await _db_context(
        conn,
        ctx,
        r,
        m,
        x,
        clean_ticker(symbol or ""),
        n0.name_compact,
        meta.candidate_words(n0, load_knowledge()),
        logo=logo_feats,
    )
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
        pair=await pair_lookup.lookup(conn, ctx.rpc, r.quote_mint),
    )
    inp.logo_features = logo_feats  # hashed above or cached: compared with other logos
    if ctx.settings.enable_embed:
        # first call loads the ONNX model from disk: keep that off the event loop
        inp.encoder = await asyncio.to_thread(embed.default_encoder, ctx.settings)
    await _queue_pair_analysis(conn, inp.pair)
    if depth == "full":
        tweet, profile = await fulldepth.x_content(conn, ctx.http, ctx.settings, x)
        inp.x_url_handle = x.ref.url_handle if x else None
        inp.tweet, inp.profile = tweet, profile
        inp.ocr_lines = await fulldepth.ocr_cached(conn, m.image_content_key if m else None)
        ran_ocr = inp.ocr_lines is None and bool(image_bytes)
        ocr_error: str | None = None
        if ran_ocr and image_bytes:
            # OCR here rather than inside run_full: waiting for the single OCR slot then
            # happens on the event loop, not on a blocked executor thread
            inp.ocr_lines, ocr_error = await ocr.read_async(image_bytes)
            inp.ocr_error = ocr_error  # keeps the 'OCR unavailable' caveat
        if ctx.settings.enable_clip:
            inp.vision = await _vision_labels(conn, ctx, m, image_bytes)
        inp.trend_index = await fulldepth.trend_index(conn, ctx.http)
        inp.x_media = await fulldepth.media_hashes(
            conn, ctx.http, ctx.settings, fulldepth.media_urls(tweet, profile)
        )
        inp.wiki_refs = await _wiki_refs(conn, ctx, inp)
        # one after the other: both use this job's single database connection
        news_hits, news_status = await _name_news(conn, ctx, inp)
        bsky_hits, bsky_status = await _name_bluesky(conn, ctx, inp)
        inp.news_hits = news_hits + bsky_hits
        # CPU-bound (normalisation, image hashing): keep it off the event loop so the
        # worker's other concurrent jobs keep making network progress meanwhile.
        out = await asyncio.to_thread(run_full, inp)
        if ran_ocr and m and m.image_content_key and not ocr_error:
            await fulldepth.persist_ocr(conn, m.image_content_key, out.ocr_lines)
        await _attach_news(conn, ctx, out)
        out.trend_sources = [*inp.trend_index.sources, news_status, bsky_status]
    else:
        out = await asyncio.to_thread(run_basic, inp)
    if x is not None:
        x.reuse_count = dbc.x_reuse_count
        x.reuse_rank = dbc.x_reuse_rank
        x.reuse_first_at = dbc.x_reuse_first_at
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

    extras = await _read_extras(conn, ctx, r, out)
    doc = build_document(r, m, depth, out, x, hint_use, extras)
    score = doc.referent.confidence if doc.referent else None
    log.info(
        "analysis.referent",
        mint=r.mint,
        depth=depth,
        status=recall.status(score),
        score=score,
        label=doc.referent.label if doc.referent else None,
    )
    await _store_xref(conn, r.mint, doc.x)
    return await _store_analysis(conn, doc)


@dataclass
class ReadExtras:
    """Facts gathered after the engine ran: the copied coins' curves now, and the waves."""

    markets: dict[str, OriginalMarket] = field(default_factory=dict)
    wave: ReferentWave | None = None
    category_waves: dict[str, int] = field(default_factory=dict)


CURVE_TIMEOUT_S = 3.0


def referent_key(label: str) -> str:
    """TokenSage's normalised referent: the resolved label, case and punctuation folded. Every
    alias resolves to one entity label first ("Elon", "Musk", "elonmusk" -> "Elon Musk")."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in label.casefold()).split())


async def _read_extras(
    conn: asyncpg.Connection, ctx: Context, r: Resolved, out: EngineOutput
) -> ReadExtras:
    ex = ReadExtras()
    now = datetime.now(UTC)
    mints = [c["mint"] for c in out.copy_of if c.get("recent") and c.get("mint")]
    if out.lineage is not None and out.lineage.original is not None:
        mints.append(out.lineage.original.mint)

    # the copied coins' curves: stored when fresh, else one batched read (1 credit for all)
    try:
        states = await curves_now(
            conn, ctx.rpc, list(dict.fromkeys(mints))[:3], rpc_timeout_s=CURVE_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001 - an optional enrichment
        log.info("lineage.curve_failed", mints=mints, error=str(e)[:120])
        states = {}
    for mint, (complete, progress, as_of) in states.items():
        ex.markets[mint] = OriginalMarket(
            complete=complete, curve_progress=progress, graduated_pool=None, as_of=as_of
        )
    launched = r.created_at

    def within(hours: int) -> int:
        return int(launched is not None and now - timedelta(hours=hours) < launched <= now)

    ref = out.agg.referent
    if ref is not None and not ref.generic:
        row = await conn.fetchrow(
            """with p as (select $3::timestamptz as now, coalesce($4::timestamptz, $3) as me)
               select count(*) filter (where launched_at > p.now - interval '1 hour') as h1,
                      count(*) filter (where launched_at > p.now - interval '6 hours') as h6,
                      count(*) filter (where launched_at > p.now - interval '24 hours') as h24,
                      count(*) filter (where launched_at > p.now - interval '24 hours'
                                         and launched_at < p.me) as before24,
                      min(launched_at) as first
               from token_read, p
               where referent_key = $1 and mint <> $2
                 and launched_at > p.now - interval '7 days' and launched_at <= p.now""",
            referent_key(ref.label),
            r.mint,
            now,
            launched,
        )
        first = row["first"]
        if launched is not None and within(24 * 7) and (first is None or launched < first):
            first = launched
        ex.wave = ReferentWave(
            launches_1h=int(row["h1"]) + within(1),
            launches_6h=int(row["h6"]) + within(6),
            launches_24h=int(row["h24"]) + within(24),
            first_seen_at=first,
            rank_24h=int(row["before24"]) + 1 if within(24) else None,
        )
    labels = [lbl for lbl, _ in out.agg.categories]
    if labels:
        rows = await conn.fetch(
            """select l, count(*) as n from token_read, unnest(categories) as l
               where launched_at > $1::timestamptz - interval '1 hour'
                 and launched_at <= $1::timestamptz
                 and mint <> $2 and l = any($3::text[])
               group by l""",
            now,
            r.mint,
            labels,
        )
        found = {row["l"]: int(row["n"]) for row in rows}
        ex.category_waves = {lbl: found.get(lbl, 0) + within(1) for lbl in labels}
    return ex


def _trend_out(out: EngineOutput) -> TrendOut:
    terms = [
        TrendTermOut(
            term=h.term.term,
            spike=h.term.spike if h.term.source == "wikipedia" else None,
            source=h.term.source,
            headline=h.headline or h.term.headline,
            score=h.score if h.score is not None else trends.score(h),
            seen_at=h.term.seen_at,
            matched_on=h.matched_on,
            searches=h.term.views if h.term.source == "google_trends" else None,
            rank=h.term.rank if h.term.source == "x_trends" else None,
            hours=h.term.views if h.term.source == "x_trends" else None,
            posts=h.term.views if h.term.source == "bluesky" else None,
            partial=True if h.partial else None,
        )
        for h in out.trend_hits
    ]
    return TrendOut(
        matched=bool(terms),
        score=max((t.score or 0.0 for t in terms), default=0.0),
        terms=terms,
        sources=[
            TrendSourceOut(
                source=s.source, status=s.status, as_of=s.as_of, terms=s.terms, detail=s.detail
            )
            for s in out.trend_sources
        ],
    )


async def _queue_pair_analysis(conn: asyncpg.Connection, pair: pairing.PairInput | None) -> None:
    """A pair token we have never analysed but that is itself a pump.fun coin: analyse it in
    the background (basic depth, lowest priority) so the next coin paired with it gets the
    pair token's full meaning (its referent and categories) instead of a read of its name."""
    if pair is None or pair.kind != "token" or pair.source not in ("db", "onchain", "none"):
        return
    if not pair.mint.endswith("pump") or pairing.xstock_ticker(pair.symbol, pair.name, pair.mint):
        return
    try:
        await queue.enqueue(conn, "analyze", pair.mint, "basic", requested_by="pair_lookup")
    except Exception as e:  # noqa: BLE001 - only a prefetch
        log.info("pair.queue_failed", mint=pair.mint, error=str(e)[:120])


async def _wiki_refs(
    conn: asyncpg.Connection, ctx: Context, inp: EngineInput
) -> list[wikilookup.WikiRef]:
    """Look up on Wikipedia the names in the coin's name and post that the gazetteer does
    not know (cached; at most wikilookup.MAX_LOOKUPS searches)."""
    from tokensage.engine.normalize import normalize

    texts: list[str] = []
    t = inp.tweet
    if t is not None and t.status == "ok":
        texts.append(t.text or "")
        for other in (t.quoted, t.replied_to):
            if other is not None and other.status == "ok":
                texts.append(other.text or "")
    try:
        n = normalize(inp.name, inp.symbol, None)
        spans = await asyncio.to_thread(
            wikilookup.spans, n, texts, load_knowledge(), inp.ctx.gazetteer
        )
    except Exception as e:  # noqa: BLE001 - an optional enrichment
        log.info("wiki.spans_failed", error=str(e)[:120])
        return []
    refs: list[wikilookup.WikiRef] = []
    titles: set[str] = set()
    found_in_name: set[str] = set()
    for span in spans:
        if any(span.text in f for f in found_in_name):
            continue  # "sydney sweeney" found: its sub-spans need no lookup
        try:
            pages = await fulldepth.wiki_search(conn, ctx.http, span.text)
        except Exception as e:  # noqa: BLE001 - an optional enrichment
            log.info("wiki.search_failed", error=str(e)[:120])
            continue
        ref = wikilookup.pick(span, pages or [])
        if ref is None or ref.title in titles:
            continue
        titles.add(ref.title)
        refs.append(ref)
        found_in_name.add(span.text)
    return refs


async def _name_news(
    conn: asyncpg.Connection, ctx: Context, inp: EngineInput
) -> tuple[list[trends.TrendHit], trends.SourceStatus]:
    """Search Google News for the coin's name (when it is specific enough to search): a coin
    named after a story that broke today is "in the news" long before, or without ever, its
    subject reaching Wikipedia's daily top 1000. Also returns the search's status."""
    phrase = gnews.name_query(inp.name)
    one_word = None
    if phrase is None:
        phrase = one_word = trends.news_word(inp.name, load_knowledge())
    if phrase is None:
        return [], trends.SourceStatus(
            "news",
            "skipped",
            detail="name not specific enough to search (needs two words, or one uncommon word)",
        )
    if not one_word and trends.ordinary(phrase, load_knowledge()):
        return [], trends.SourceStatus(
            "news",
            "skipped",
            detail=f"'{phrase}' is everyday words: some headline has it whatever is trending",
        )
    try:
        found = await fulldepth.news_lookup(conn, ctx.http, phrase, exact=True)
    except Exception as e:  # noqa: BLE001 - an optional enrichment
        log.info("news.lookup_failed", error=str(e)[:120])
        return [], trends.SourceStatus("news", "failed", detail=f"{type(e).__name__}")
    if found is None:
        return [], trends.SourceStatus("news", "failed", detail="Google News unavailable")
    try:
        rel = gnews.relevant(found.headlines, phrase, inp.symbol)
        hit = trends.news_hit(
            phrase,
            rel,
            min_outlets=trends.MIN_ONE_WORD_OUTLETS if one_word else trends.MIN_NEWS_HEADLINES,
        )
    except Exception as e:  # noqa: BLE001 - headlines are third-party data; never fail the coin
        log.info("news.parse_failed", error=f"{type(e).__name__}: {e}"[:120])
        return [], trends.SourceStatus("news", "failed", detail=f"{type(e).__name__}")
    st = trends.SourceStatus(
        "news",
        "stale" if found.stale else "ok",
        as_of=found.as_of,
        terms=len(rel),
        detail=f"searched '{phrase}'"
        + (f"; one word, needs {trends.MIN_ONE_WORD_OUTLETS} outlets" if one_word else "")
        + ("; Google News unavailable, older cached headlines" if found.stale else ""),
    )
    return ([hit] if hit else []), st


async def _name_bluesky(
    conn: asyncpg.Connection, ctx: Context, inp: EngineInput
) -> tuple[list[trends.TrendHit], trends.SourceStatus]:
    """Search Bluesky for the coin's name (the same names Google News is searched for): a
    subject people are posting about today, before or without any headline. Posts about a
    coin (cashtags, prices, pump.fun) do not count."""
    phrase = gnews.name_query(inp.name)
    if phrase is None:
        return [], trends.SourceStatus(
            "bluesky",
            "skipped",
            detail="name not specific enough to search (needs two words; one-word names are "
            "not searched on Bluesky)",
        )
    if trends.ordinary(phrase, load_knowledge()):
        return [], trends.SourceStatus(
            "bluesky",
            "skipped",
            detail=f"'{phrase}' is everyday words: people post it whatever is trending",
        )
    try:
        found = await fulldepth.bsky_for(conn, ctx.http, phrase)
    except Exception as e:  # noqa: BLE001 - an optional enrichment
        log.info("bluesky.lookup_failed", error=str(e)[:120])
        return [], trends.SourceStatus("bluesky", "failed", detail=f"{type(e).__name__}")
    if found is None:
        return [], trends.SourceStatus("bluesky", "failed", detail="Bluesky unavailable")
    try:
        rel = bluesky.relevant(found.posts, phrase, inp.symbol)
        hit = trends.bluesky_hit(phrase, rel)
    except Exception as e:  # noqa: BLE001 - posts are third-party JSON; never fail the coin
        log.info("bluesky.parse_failed", error=f"{type(e).__name__}: {e}"[:120])
        return [], trends.SourceStatus("bluesky", "failed", detail=f"{type(e).__name__}")
    st = trends.SourceStatus(
        "bluesky",
        "stale" if found.stale else "ok",
        as_of=found.as_of,
        terms=hit.term.views if hit else 0,
        detail=f"searched '{phrase}'"
        + ("; Bluesky unavailable, older cached posts" if found.stale else ""),
    )
    return ([hit] if hit else []), st


async def _attach_news(conn: asyncpg.Connection, ctx: Context, out: EngineOutput) -> None:
    """Confirm the top Wikipedia trend hits against Google News: a caveat with the headline
    count, and the first headline on the hit (trend.terms[].headline)."""
    wiki = [h for h in out.trend_hits if h.term.source not in ("news", "bluesky")]
    hits = sorted(wiki, key=lambda h: -h.term.spike)[: fulldepth.MAX_NEWS_LOOKUPS]
    for h in hits:
        heads = await fulldepth.news_for(conn, ctx.http, h.term.term)
        if heads is None:
            continue
        if heads:
            h.headline = str(heads[0]["title"])[:200]
            out.caveats.append(
                f"news check: {len(heads)} recent headline(s) for '{h.term.term}', e.g. "
                f'"{heads[0]["title"][:90]}"'
            )
        else:
            out.caveats.append(f"news check: no recent headlines found for '{h.term.term}'")


class RetryRescheduled(Exception):
    """The retry_metadata job put itself back in the queue for its next attempt."""


async def retry_metadata(
    conn: asyncpg.Connection, ctx: Context, mint: str, depth: str, job_id: int | None = None
) -> int | None:
    """Background job: try the metadata again; on success re-run the analysis. If it is
    still unresolved and more retries are due, raises RetryRescheduled (job re-queued)."""
    row = await conn.fetchrow("select uri from token where mint=$1", mint)
    if not row or not row["uri"]:
        return None
    attempts = await _metadata_attempts(conn, mint)
    m = await md.fetch_metadata(ctx.http, row["uri"], ctx.settings)
    await md.persist(conn, mint, m, attempts + (1 if m.status != "ok" else 0))
    if m.status == "ok":
        return await analyze(conn, ctx, mint, depth)
    if m.status == "unresolved" and await _schedule_retry(conn, mint, depth, own_job_id=job_id):
        raise RetryRescheduled
    return None
