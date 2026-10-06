"""Cron entrypoint (daily): refresh knowledge tables.

- Wikipedia pageviews: top-1000 per day with spike ratios -> trend_term (guide §5.8)
- CoinGecko meme categories -> known_coin, with logo pHash (weekly)
- prune old trend rows
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import structlog

from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.engine import image as image_stage
from tokensage.logging import configure_logging
from tokensage.net.safe_fetch import FetchError, UnsafeUrl, safe_get
from tokensage.sources import coingecko, wikimedia

log = structlog.get_logger("jobs.knowledge")

TREND_DAYS = 8
TREND_KEEP_DAYS = 45
COINGECKO_EVERY = timedelta(days=6)
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
        "select id from known_coin where source='coingecko' and logo_phash is null limit $1",
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
        except (FetchError, UnsafeUrl, ValueError, OSError):
            continue
        await conn.execute("update known_coin set logo_phash=$2 where id=$1", r["id"], feats.phash)
        hashed += 1
    return {"upserted": upserted, "logos_hashed": hashed}


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await create_pool(settings.database_url, min_size=1, max_size=2)
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.http_user_agent}, follow_redirects=False
    ) as http:
        try:
            async with pool.acquire() as conn:
                t = await refresh_trends(conn, http)
                log.info("knowledge.trends", **t)
                c = await refresh_known_coins(conn, http, settings)
                log.info("knowledge.known_coins", **c)
        finally:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
