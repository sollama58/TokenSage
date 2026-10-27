"""The `now` refresh (rules 0.34.0): a coin's trend block matched against the current trend
index, and how its story trades right now, for a consumer deciding on the coin some time after
its full read (POST /v1/tokens:now).

A full read freezes the trend terms at the moment it ran, and a consumer that keeps the read
(TrenchScanner never asks again once it has one) decides on old trends. The refresh re-runs
only the trend match, on the stored read's identity (name, ticker, description, X post and
referent; the name searches are not repeated), and looks up the story's leading coin on
GeckoTerminal. Results are kept in lookup_cache ("now:<mint>", "heat:<story>").
"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage import fulldepth
from tokensage.api.schemas import Analysis, HeatLeader, StoryHeat, Trend
from tokensage.engine import trends
from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.sources import geckoterminal
from tokensage.versions import NOW_KEY, RULES_VERSION, TREND_INPUTS_KEY

log = structlog.get_logger("now")

HEAT_KEY = "heat:"  # lookup_cache: a story's heat, shared by every coin on it
HEAT_TTL = timedelta(minutes=5)
HEAT_STALE_MAX = 2 * HEAT_TTL  # a failed refresh falls back to cached heat only this young
HEAT_FAILED_TTL = timedelta(minutes=3)  # retry a failed or rate-limited lookup soon

# A failed refresh that fell back to a recent cached heat writes nothing, so the coins waiting
# on the story's single flight each searched again in turn (audit 2026-10-23 TS-3). The
# leader's outcome is kept here, per process, until HEAT_FAILED_TTL or until the stale row
# passes HEAT_STALE_MAX, whichever is first; waiters take it instead of searching.
_HEAT_FALLBACK_MAX = 2000
_heat_fallback: dict[str, tuple[float, StoryHeat]] = {}


def _fallback_get(key: str) -> StoryHeat | None:
    hit = _heat_fallback.get(key)
    if hit is None:
        return None
    if hit[0] <= time.monotonic():
        _heat_fallback.pop(key, None)
        return None
    return hit[1].model_copy(deep=True)


def _fallback_keep(key: str, heat: StoryHeat, row_age: timedelta) -> None:
    ttl = min(HEAT_FAILED_TTL, HEAT_STALE_MAX - row_age).total_seconds()
    if ttl <= 0:
        return
    _heat_fallback.pop(key, None)
    _heat_fallback[key] = (time.monotonic() + ttl, heat.model_copy(deep=True))
    while len(_heat_fallback) > _HEAT_FALLBACK_MAX:
        _heat_fallback.pop(next(iter(_heat_fallback)))


# a lineage original counts as the story only for these kinds: an "original" has none
LINEAGE_STORY_KINDS = ("early_copy", "copy", "late_copy", "reference")
MIN_STORY_LEN = 3
# story heat counts pump.fun coins only (its bonding curve, PumpSwap, or a pump.fun mint
# trading elsewhere), in pools opened this recently: the wave, not an older namesake
PUMP_DEXES = ("pump-fun", "pumpswap")
STORY_POOL_MAX_AGE = timedelta(days=7)

# Ev.where values a stored referent may carry (engine.context.Where)
REFERENT_WHERE = ("name", "symbol", "description", "image", "x", "trend", "chain", "db", "copy_of")

_LATEST_FULL_SQL = """select doc from analysis where mint=$1 and depth='full'
    order by version desc limit 1"""


def _compact(s: str | None) -> str:
    folded = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _parse_dt(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


# How long a name-search hit from the full read still counts in a refresh: the window each
# search covers in the full read itself.
_NAME_SEARCH_WINDOWS = {"news": fulldepth.NEWS_WINDOW, "bluesky": trends.BLUESKY_WINDOW}


def _name_search_hits(doc: dict[str, Any], now: datetime | None = None) -> list[trends.TrendHit]:
    """The full read's Google News and Bluesky hits, from its trend block: the name searches
    are not repeated, but what they found stays in the match as it was in the full read, for as
    long as the full read's own search window would still have found it. Past that a refresh
    stamped "now" would report a days-old headline as trending (audit 2026-10-19). A hit with
    no date of its own is dated by the read."""
    now = now or datetime.now(UTC)
    read_at = _parse_dt(doc.get("analyzed_at"))
    out: list[trends.TrendHit] = []
    for t in (doc.get("trend") or {}).get("terms") or []:
        if not isinstance(t, dict) or t.get("source") not in ("news", "bluesky"):
            continue
        term = str(t.get("term") or "")
        if not term:
            continue
        seen = _parse_dt(t.get("seen_at")) or read_at
        if seen is not None and now - seen >= _NAME_SEARCH_WINDOWS[str(t["source"])]:
            continue
        if t["source"] == "bluesky":
            views = int(t.get("posts") or 0)
        else:  # trends.score: headlines / 8, at most 1
            views = round(float(t.get("score") or 0.0) * 8)
        out.append(
            trends.TrendHit(
                term=trends.TrendTerm(
                    term,
                    0.0,
                    views,
                    str(t["source"]),
                    seen_at=_parse_dt(t.get("seen_at")),
                ),
                surface=term.lower(),
                where="name",
                headline=t.get("headline"),
            )
        )
    return out


def _referent_evidence(doc: dict[str, Any], stored: list[dict[str, Any]] | None) -> list[Ev]:
    """The referents the full read matched against the trends (TREND_INPUTS_KEY, written by
    the analyzer), else the read's chosen referent alone (reads from before that row)."""
    rows = stored
    if rows is None:
        ref = doc.get("referent")
        rows = []
        if isinstance(ref, dict) and ref.get("label") and not ref.get("generic"):
            rows = [
                {
                    "label": ref["label"],
                    "kind": ref.get("kind"),
                    "desc": ref.get("desc"),
                    "source": ref.get("source"),
                    "score": ref.get("confidence"),
                    "where": (ref.get("supported_by") or ["name"])[0],
                }
            ]
    evidence: list[Ev] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("label"):
            continue
        where = r.get("where")
        cand = ReferentCandidate(
            label=str(r["label"]),
            kind=str(r.get("kind") or "other"),
            desc=r.get("desc"),
            source=str(r.get("source") or ""),
            score=float(r.get("score") or 0.5),
        )
        evidence.append(
            Ev(
                kind="referent",
                label="referent",
                weight=cand.score,
                detail="stored referent",
                source=cand.source,
                where=where if where in REFERENT_WHERE else "name",
                referent=cand,
            )
        )
    return evidence


def fresh_trend(
    doc: dict[str, Any],
    index: trends.TrendIndex,
    referents: list[dict[str, Any]] | None = None,
) -> Trend:
    """The stored read's trend block, matched again against `index` on the full read's
    inputs: its name, description, X post and the posts it quotes or replies to, its logo
    text, its name-search hits and its referents (`referents`, from TREND_INPUTS_KEY; the
    chosen referent alone when None). Audit 2026-10-14 M5: the refresh had used the name,
    description, post and one referent only, so `matched` could flip by construction."""
    from tokensage.analyzer import trend_block

    k = load_knowledge()
    raw = doc.get("raw") or {}
    n = normalize(raw.get("name"), raw.get("symbol"), raw.get("description"))
    texts = [("name", " ".join(n.name_tokens)), ("description", n.desc_clean)]
    x = doc.get("x") or {}
    if isinstance(x, dict):
        if x.get("text"):
            texts.append(("x", str(x["text"])))
        for rel in (x.get("quoted"), x.get("replied_to")):
            if isinstance(rel, dict) and rel.get("text"):
                texts.append(("x", str(rel["text"])))
    ocr_lines = (doc.get("image") or {}).get("ocr") or []
    ocr_text = ""
    if ocr_lines:
        ocr_text = " ".join(normalize(" ".join(map(str, ocr_lines)), "", None).name_tokens)
    hits = trends.collect(
        index,
        k,
        texts,
        name_tokens=n.name_tokens,
        name_compact=n.name_compact,
        ticker=n.ticker,
        evidence=_referent_evidence(doc, referents),
        news_hits=_name_search_hits(doc),
        extra_support=ocr_text,
    )
    return trend_block(hits, list(index.sources))


def story_of(doc: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """(phrase to search, "lineage" | "referent", the lineage original's mint) or None."""
    lin = doc.get("lineage")
    if isinstance(lin, dict) and lin.get("kind") in LINEAGE_STORY_KINDS:
        phrase = lin.get("of_name") or lin.get("of_ticker")
        if phrase and len(_compact(phrase)) >= MIN_STORY_LEN:
            return str(phrase), "lineage", lin.get("of_mint")
    ref = doc.get("referent")
    if isinstance(ref, dict) and ref.get("label") and not ref.get("generic"):
        label = str(ref["label"])
        if len(_compact(label)) >= MIN_STORY_LEN:
            return label, "referent", None
    return None


def _recent(q: geckoterminal.PoolQuote, now: datetime) -> bool:
    if not q.created_at:
        return False
    try:
        created = datetime.fromisoformat(q.created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return now - created <= STORY_POOL_MAX_AGE


def _on_story(
    q: geckoterminal.PoolQuote, story: str, story_mint: str | None, now: datetime | None = None
) -> bool:
    """Whether the pool's coin is on the story: the lineage original itself, or a pump.fun
    coin in a pool opened within STORY_POOL_MAX_AGE whose whole name or ticker is the story.
    A name that only contains it ("cat" in POPCAT) and an established coin of another
    launchpad (WIF, the official TRUMP) are not the story's leader (audit 2026-10-14 M4)."""
    if story_mint and q.mint == story_mint:
        return True
    c = _compact(story)
    if not c or not (_compact(q.name) == c or _compact(q.symbol) == c):
        return False
    pump = q.dex in PUMP_DEXES or q.mint.endswith("pump")
    return pump and _recent(q, now or datetime.now(UTC))


def heat_from_pools(
    pools: list[geckoterminal.PoolQuote],
    story: str,
    kind: str,
    story_mint: str | None,
) -> tuple[StoryHeat, geckoterminal.PoolQuote | None]:
    """The coins on the story (one row per mint, pools summed), their leader by market cap.
    The coin asking may be among them, and may be the leader."""
    by: dict[str, geckoterminal.PoolQuote] = {}
    now = datetime.now(UTC)
    for q in pools:
        if not _on_story(q, story, story_mint, now):
            continue
        have = by.get(q.mint)
        if have is None:
            by[q.mint] = geckoterminal.PoolQuote(**q.__dict__)
            continue
        busier = q.volume_1h_usd > have.volume_1h_usd
        have.volume_1h_usd += q.volume_1h_usd
        have.volume_5m_usd += q.volume_5m_usd
        if busier:
            have.pool, have.change_5m_pct, have.change_1h_pct = (
                q.pool,
                q.change_5m_pct,
                q.change_1h_pct,
            )
        have.mcap_usd = max(have.mcap_usd or 0.0, q.mcap_usd or 0.0) or None
    coins = list(by.values())
    lead = max(coins, key=lambda c: c.mcap_usd or 0.0, default=None)
    heat = StoryHeat(
        story=story,
        story_kind=kind,  # type: ignore[arg-type]
        status="ok",
        as_of=datetime.now(UTC),
        coins=len(coins),
        leader=None
        if lead is None
        else HeatLeader(
            mint=lead.mint,
            name=lead.name,
            symbol=lead.symbol,
            pool=lead.pool,
            mcap_usd=lead.mcap_usd,
            change_5m_pct=lead.change_5m_pct,
            change_1h_pct=lead.change_1h_pct,
            volume_5m_usd=round(lead.volume_5m_usd, 2),
            volume_1h_usd=round(lead.volume_1h_usd, 2),
        ),
        wave_volume_1h_usd=round(sum(c.volume_1h_usd for c in coins), 2),
        wave_volume_5m_usd=round(sum(c.volume_5m_usd for c in coins), 2),
    )
    return heat, lead


async def story_heat(
    conn: asyncpg.Connection, http: httpx.AsyncClient, doc: dict[str, Any]
) -> StoryHeat | None:
    found = story_of(doc)
    if found is None:
        return None
    story, kind, story_mint = found
    # v2: exact pump.fun matches only (audit 2026-10-14 M4); older rows matched substrings.
    # v3: the lineage original is part of the key, so stories that share a name but not an
    # original don't share a leader
    key = f"{HEAT_KEY}v3:{kind}:{_compact(story)}:{story_mint or ''}"
    # single flight: the coins of one story refresh together, and only the first should spend
    # the GeckoTerminal search and peak (10 calls a minute); the others find its heat cached
    async with fulldepth._single_flight(key):
        return await _story_heat(conn, http, story, kind, story_mint, key)


async def _story_heat(
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    story: str,
    kind: str,
    story_mint: str | None,
    key: str,
) -> StoryHeat:
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and isinstance(row["value"], dict):
        cached = StoryHeat.model_validate(row["value"])
        ttl = HEAT_TTL if cached.status == "ok" else HEAT_FAILED_TTL
        if datetime.now(UTC) - row["fetched_at"] < ttl:
            return cached
    shared = _fallback_get(key)
    if shared is not None:
        return shared  # the single flight's leader just failed and fell back to this
    heat: StoryHeat
    try:
        pools = await geckoterminal.search_pools(http, story)
        if pools is None:
            heat = StoryHeat(story=story, story_kind=kind, status="failed", as_of=None)  # type: ignore[arg-type]
        else:
            # every coin on the story shares the cached heat, so none is left out as "self":
            # a coin that leads its own story finds its mint in leader.mint
            heat, lead = heat_from_pools(pools, story, kind, story_mint)
            if lead is not None and heat.leader is not None and lead.pool:
                # A 429 on the peak alone keeps the search's heat, without the peak timing;
                # it used to throw the paid-for search away.
                try:
                    peak = await geckoterminal.pool_peak(http, lead.pool)
                except geckoterminal.RateLimited:
                    peak = None
                if peak is not None:
                    heat.leader.peak_minutes_ago = peak.minutes_ago
                    heat.leader.from_peak_pct = peak.from_peak_pct
    except geckoterminal.RateLimited:
        heat = StoryHeat(story=story, story_kind=kind, status="rate_limited", as_of=None)  # type: ignore[arg-type]
    if (
        heat.status != "ok"
        and row
        and isinstance(row["value"], dict)
        and datetime.now(UTC) - row["fetched_at"] <= HEAT_STALE_MAX
    ):
        stale = StoryHeat.model_validate(row["value"])  # the last good one, if still recent
        _fallback_keep(key, stale, datetime.now(UTC) - row["fetched_at"])
        return stale
    _heat_fallback.pop(key, None)
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        heat.model_dump(mode="json"),
    )
    return heat


async def _store(conn: asyncpg.Connection, mint: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        NOW_KEY + mint,
        value,
    )


def body_from_read(doc: Analysis) -> dict[str, Any]:
    """The NowItem body a full read itself yields (since rules 0.37.0): its trend block is the
    match of its moment, and its story_heat the lookup the read just made. Stored under
    now:<mint> by the analyzer, so a consumer that asks for the refresh right after the read
    (TrenchScanner does, for every band coin) is served at once instead of starting a job that
    would redo both (model audit 2026-10-26 §16)."""
    return {
        "ca": doc.mint,
        "status": "complete",
        "as_of": doc.analyzed_at.isoformat(),
        "trend": doc.trend.model_dump(mode="json"),
        "story_heat": doc.story_heat.model_dump(mode="json") if doc.story_heat else None,
        "rules": RULES_VERSION,
    }


async def store_from_read(conn: asyncpg.Connection, doc: Analysis) -> None:
    """Keep a full read's own trend and heat as the mint's current refresh (body_from_read)."""
    await _store(conn, doc.mint, body_from_read(doc))
    log.info("now.from_read", mint=doc.mint)


async def refresh(conn: asyncpg.Connection, http: httpx.AsyncClient, mint: str) -> dict[str, Any]:
    """Refresh one mint and store it under now:<mint>. The stored value is a NowItem body."""
    row = await conn.fetchrow(_LATEST_FULL_SQL, mint)
    now = datetime.now(UTC)
    if row is None:
        value: dict[str, Any] = {
            "ca": mint,
            "status": "unavailable",
            "as_of": now.isoformat(),
            "error": "no_full_read",
            "rules": RULES_VERSION,
        }
    else:
        doc = row["doc"]
        index = await fulldepth.trend_index(conn, http)
        inputs = await conn.fetchval(
            "select value from lookup_cache where key=$1", TREND_INPUTS_KEY + mint
        )
        refs = inputs.get("referents") if isinstance(inputs, dict) else None
        trend = fresh_trend(doc, index, refs if isinstance(refs, list) else None)
        heat = await story_heat(conn, http, doc)
        value = {
            "ca": mint,
            "status": "complete",
            "as_of": now.isoformat(),
            "trend": trend.model_dump(mode="json"),
            "story_heat": heat.model_dump(mode="json") if heat else None,
            "rules": RULES_VERSION,
        }
    await _store(conn, mint, value)
    log.info("now.refreshed", mint=mint, status=value["status"])
    return value
