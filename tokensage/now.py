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
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage import fulldepth
from tokensage.api.schemas import HeatLeader, StoryHeat, Trend
from tokensage.engine import trends
from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.sources import geckoterminal
from tokensage.versions import NOW_KEY, RULES_VERSION

log = structlog.get_logger("now")

HEAT_KEY = "heat:"  # lookup_cache: a story's heat, shared by every coin on it
HEAT_TTL = timedelta(minutes=5)
HEAT_FAILED_TTL = timedelta(minutes=1)  # retry a failed or rate-limited lookup soon
# a lineage original counts as the story only for these kinds: an "original" has none
LINEAGE_STORY_KINDS = ("early_copy", "copy", "late_copy", "reference")
MIN_STORY_LEN = 3

_LATEST_FULL_SQL = """select doc from analysis where mint=$1 and depth='full'
    order by version desc limit 1"""


def _compact(s: str | None) -> str:
    folded = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def fresh_trend(doc: dict[str, Any], index: trends.TrendIndex) -> Trend:
    """The stored read's trend block, matched again against `index`."""
    from tokensage.analyzer import trend_block

    k = load_knowledge()
    raw = doc.get("raw") or {}
    n = normalize(raw.get("name"), raw.get("symbol"), raw.get("description"))
    texts = [("name", " ".join(n.name_tokens)), ("description", n.desc_clean)]
    x = doc.get("x") or {}
    if isinstance(x, dict) and x.get("text"):
        texts.append(("x", str(x["text"])))
    evidence: list[Ev] = []
    ref = doc.get("referent")
    if isinstance(ref, dict) and ref.get("label") and not ref.get("generic"):
        where = (ref.get("supported_by") or ["name"])[0]
        cand = ReferentCandidate(
            label=str(ref["label"]),
            kind=str(ref.get("kind") or "other"),
            desc=ref.get("desc"),
            source=str(ref.get("source") or ""),
            score=float(ref.get("confidence") or 0.5),
        )
        evidence.append(
            Ev(
                kind="referent",
                label="referent",
                weight=cand.score,
                detail="stored referent",
                source=cand.source,
                where=where if where in ("name", "symbol", "description", "x") else "name",
                referent=cand,
            )
        )
    hits = trends.collect(
        index,
        k,
        texts,
        name_tokens=n.name_tokens,
        name_compact=n.name_compact,
        ticker=n.ticker,
        evidence=evidence,
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


def _on_story(q: geckoterminal.PoolQuote, story: str, story_mint: str | None) -> bool:
    if story_mint and q.mint == story_mint:
        return True
    c = _compact(story)
    return bool(c) and (c in _compact(q.name) or _compact(q.symbol) == c)


def heat_from_pools(
    pools: list[geckoterminal.PoolQuote],
    story: str,
    kind: str,
    story_mint: str | None,
) -> tuple[StoryHeat, geckoterminal.PoolQuote | None]:
    """The coins on the story (one row per mint, pools summed), their leader by market cap.
    The coin asking may be among them, and may be the leader."""
    by: dict[str, geckoterminal.PoolQuote] = {}
    for q in pools:
        if not _on_story(q, story, story_mint):
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
    key = f"{HEAT_KEY}{kind}:{_compact(story)}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and isinstance(row["value"], dict):
        cached = StoryHeat.model_validate(row["value"])
        ttl = HEAT_TTL if cached.status == "ok" else HEAT_FAILED_TTL
        if datetime.now(UTC) - row["fetched_at"] < ttl:
            return cached
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
                peak = await geckoterminal.pool_peak(http, lead.pool)
                if peak is not None:
                    heat.leader.peak_minutes_ago = peak.minutes_ago
                    heat.leader.from_peak_pct = peak.from_peak_pct
    except geckoterminal.RateLimited:
        heat = StoryHeat(story=story, story_kind=kind, status="rate_limited", as_of=None)  # type: ignore[arg-type]
    if heat.status != "ok" and row and isinstance(row["value"], dict):
        return StoryHeat.model_validate(row["value"])  # the last good one
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        heat.model_dump(mode="json"),
    )
    return heat


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
        trend = fresh_trend(doc, index)
        heat = await story_heat(conn, http, doc)
        value = {
            "ca": mint,
            "status": "complete",
            "as_of": now.isoformat(),
            "trend": trend.model_dump(mode="json"),
            "story_heat": heat.model_dump(mode="json") if heat else None,
            "rules": RULES_VERSION,
        }
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        NOW_KEY + mint,
        value,
    )
    log.info("now.refreshed", mint=mint, status=value["status"])
    return value
