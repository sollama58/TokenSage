"""Cron entrypoint (daily): refresh knowledge tables.

- Wikipedia pageviews: top-1000 per day with spike ratios -> trend_term (guide §5.8)
- CoinGecko meme categories -> known_coin, with logo pHash (weekly)
- Wikidata people, animals, memes, AI bots, pop culture -> entity (monthly, guide §4.4)
- GeckoTerminal: the day's most-traded pump.fun tokens -> top_volume (daily, guide §5.5)
- prune old trend rows
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import structlog

from tokensage import gazetteer_db
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.engine import image as image_stage
from tokensage.engine.knowledge import load_knowledge
from tokensage.logging import configure_logging
from tokensage.net import metrics
from tokensage.net.safe_fetch import safe_get
from tokensage.sources import coingecko, geckoterminal, wikidata, wikimedia

log = structlog.get_logger("jobs.knowledge")

TREND_DAYS = 8
TREND_KEEP_DAYS = 45
COINGECKO_EVERY = timedelta(days=6)
GAZETTEER_EVERY = timedelta(days=28)
LOGO_HASHES_PER_RUN = 150


async def refresh_trends(conn: asyncpg.Connection, http: httpx.AsyncClient) -> dict[str, int]:
    have = {
        r["day"]
        for r in await conn.fetch(
            "select distinct day from trend_term where source='wikipedia' and day >= $1",
            date.today() - timedelta(days=TREND_KEEP_DAYS),
        )
    }
    history: dict[date, dict[str, int]] = {}
    for r in await conn.fetch(
        "select day, term, views from trend_term where source='wikipedia' and day >= $1",
        date.today() - timedelta(days=31),
    ):
        history.setdefault(r["day"], {})[r["term"]] = int(r["views"] or 0)
    fetched = inserted = 0
    for day in sorted(wikimedia.days_back(TREND_DAYS)):
        if day in have:
            continue
        top = await wikimedia.top_articles(http, day)
        if top is None:
            log.info("trends.day_unavailable", day=str(day))
            continue
        fetched += 1
        prior = {d: m for d, m in history.items() if d < day}
        spikes = wikimedia.spike_ratios(top, prior)
        rows = [
            (title, "wikipedia", float(views), spikes.get(title, 1.0), day, day, int(views))
            for title, views in top.items()
        ]
        await conn.executemany(
            """insert into trend_term (term, source, score, spike, first_seen, day, views)
               values ($1,$2,$3,$4,$5,$6,$7)
               on conflict (term, source, day) do update set score=excluded.score,
                 spike=excluded.spike, views=excluded.views""",
            rows,
        )
        inserted += len(rows)
        history[day] = top
    pruned = await conn.execute(
        "delete from trend_term where day < $1", date.today() - timedelta(days=TREND_KEEP_DAYS)
    )
    return {"days_fetched": fetched, "rows": inserted, "pruned": int(pruned.split()[-1])}


async def refresh_known_coins(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, force: bool = False
) -> dict[str, int]:
    last = await conn.fetchval("select max(updated_at) from known_coin where source='coingecko'")
    if last and not force and datetime.now(UTC) - last < COINGECKO_EVERY:
        return {"skipped": 1}
    upserted = 0
    seen: dict[str, coingecko.GeckoCoin] = {}
    for cat in coingecko.CATEGORIES:
        coins = await coingecko.category_markets(http, settings.coingecko_api_key, cat)
        if coins is None:
            log.warning("coingecko.unavailable", category=cat)
            continue
        for c in coins:
            if c.id in seen:
                seen[c.id].categories = sorted(set(seen[c.id].categories) | set(c.categories))
            else:
                seen[c.id] = c
        await asyncio.sleep(1.5)  # demo key: stay far under 30 req/min
    for c in seen.values():
        await conn.execute(
            """insert into known_coin (id, chain, symbol, name, aliases, lore, categories, source,
                                       updated_at)
               values ($1, 'any', $2, $3, '{}', $4, $5, 'coingecko', now())
               on conflict (id) do update set symbol=excluded.symbol, name=excluded.name,
                 categories=excluded.categories, updated_at=now()""",
            f"coingecko:{c.id}",
            c.symbol,
            c.name,
            f"CoinGecko {', '.join(c.categories) or 'meme'} coin (rank {c.market_cap_rank})",
            c.categories,
        )
        upserted += 1
    # logo hashes for coins that don't have one yet
    hashed = 0
    rows = await conn.fetch(
        # random order: coins whose logo never fetches must not fill every batch forever
        """select id from known_coin where source='coingecko' and logo_phash is null
           order by random() limit $1""",
        LOGO_HASHES_PER_RUN,
    )
    by_id = {f"coingecko:{c.id}": c for c in seen.values()}
    for r in rows:
        gc = by_id.get(r["id"])
        if not gc or not gc.image:
            continue
        try:
            f = await safe_get(http, gc.image, max_bytes=2_000_000, timeout=10.0, accept="image/*")
            feats = image_stage.features(f.body)
        except Exception:  # noqa: BLE001 - incl. PIL DecompressionBombError; skip this logo
            continue
        await conn.execute("update known_coin set logo_phash=$2 where id=$1", r["id"], feats.phash)
        hashed += 1
    return {"upserted": upserted, "logos_hashed": hashed}


async def refresh_gazetteer(
    conn: asyncpg.Connection, http: httpx.AsyncClient, force: bool = False
) -> dict[str, int]:
    """The Wikidata gazetteer, monthly. Takes several minutes (one SPARQL query at a time)."""
    last = await conn.fetchval(
        "select max(updated_at) from entity where source=$1", gazetteer_db.SOURCE
    )
    if last and not force and datetime.now(UTC) - last < GAZETTEER_EVERY:
        return {"skipped": 1}
    entities, failed = await wikidata.fetch_all(http)
    if failed:
        log.warning("gazetteer.groups_failed", groups=failed)
    out = await gazetteer_db.store(conn, entities, complete=not failed)
    return {**out, "groups_failed": len(failed)}


async def refresh_top_volume(
    conn: asyncpg.Connection, http: httpx.AsyncClient, pause_s: float = 6.0
) -> dict[str, int]:
    """Today's snapshot of the most-traded pump.fun tokens (data/meta.yaml top_volume).
    A failed fetch keeps the previous days; nothing is stored for today."""
    cfg = load_knowledge().meta.get("top_volume") or {}
    top = await geckoterminal.top_tokens(
        http,
        [str(d) for d in cfg.get("dexes") or ["pump-fun", "pumpswap"]],
        int(cfg.get("pages", 2)),
        int(cfg.get("count", 25)),
        pause_s=pause_s,
    )
    if not top:
        log.warning("top_volume.unavailable")
        return {"stored": 0}
    today = datetime.now(UTC).date()
    async with conn.transaction():
        await conn.execute("delete from top_volume where day=$1", today)
        await conn.executemany(
            """insert into top_volume (day, rank, mint, name, symbol, volume_usd, dex)
               values ($1,$2,$3,$4,$5,$6,$7)""",
            [
                (today, i, t.mint, t.name, t.symbol, t.volume_usd, t.dex)
                for i, t in enumerate(top, 1)
            ],
        )
    pruned = await conn.execute(
        "delete from top_volume where day < $1",
        today - timedelta(days=int(cfg.get("keep_days", 45))),
    )
    return {"stored": len(top), "pruned": int(pruned.split()[-1])}


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    metrics.meter.configure(
        ipfs_gateways=settings.ipfs_gateway_list, costs=settings.helius_credit_costs
    )
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=False,
        transport=metrics.MeteredTransport(),
    ) as http:
        try:
            async with pool.acquire() as conn:
                t = await refresh_trends(conn, http)
                log.info("knowledge.trends", **t)
                c = await refresh_known_coins(conn, http, settings)
                log.info("knowledge.known_coins", **c)
                v = await refresh_top_volume(conn, http)
                log.info("knowledge.top_volume", **v)
                g = await refresh_gazetteer(conn, http)
                log.info("knowledge.gazetteer", **g)
        finally:
            try:
                async with pool.acquire() as conn:
                    await metrics.flush(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("metrics.flush_failed", error=f"{type(e).__name__}: {e}")
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
