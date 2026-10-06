"""The Wikidata gazetteer in Postgres: the knowledge cron's monthly refresh writes it, the
analyzer loads it (guide §4.4). Until the first refresh lands, the packaged snapshot in
data/ is used, so a fresh deploy already knows every entity in it."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import asyncpg
import structlog

from tokensage.engine import gazetteer
from tokensage.engine.gazetteer import GazEntry, Gazetteer
from tokensage.engine.knowledge import load_knowledge
from tokensage.sources.wikidata import WikiEntity

log = structlog.get_logger("gazetteer")

SOURCE = "wikidata"
CHECK_EVERY_S = 900  # how often a worker asks whether the table changed
MIN_ROWS = 1000  # fewer rows than this is a broken refresh, not a gazetteer
# A refresh returning fewer than this share of the stored rows is a partial outage of the
# query service: keep the old rows rather than shrink the gazetteer.
MIN_KEEP_SHARE = 0.6

_lock = asyncio.Lock()
_cache: tuple[float, str, Gazetteer] | None = None


async def current(conn: asyncpg.Connection) -> Gazetteer:
    """The gazetteer to match against: the table's when it is populated, else the packaged
    snapshot. Rebuilt only when the table changes; the check runs every CHECK_EVERY_S."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CHECK_EVERY_S:
        return _cache[2]
    async with _lock:
        if _cache and now - _cache[0] < CHECK_EVERY_S:
            return _cache[2]
        row = await conn.fetchrow(
            "select count(*) n, max(updated_at) u from entity where source=$1", SOURCE
        )
        n, updated = int(row["n"]), row["u"]
        sig = f"db:{n}:{updated.isoformat() if updated else ''}" if n >= MIN_ROWS else "packaged"
        if _cache and _cache[1] == sig:
            _cache = (now, sig, _cache[2])
            return _cache[2]
        if sig == "packaged":
            g = await asyncio.to_thread(gazetteer.packaged)
        else:
            rows = await conn.fetch(
                """select id, label, aliases, kind, description, categories, sitelinks
                   from entity where source=$1""",
                SOURCE,
            )
            entries = [
                GazEntry(
                    id=str(r["id"]).removeprefix("wikidata:"),
                    label=r["label"] or "",
                    aliases=tuple(r["aliases"] or ()),
                    desc=r["description"] or "",
                    kind=r["kind"] or "other",
                    categories=tuple(r["categories"] or ()),
                    sitelinks=int(r["sitelinks"] or 0),
                )
                for r in rows
            ]
            version = f"wikidata-{updated:%Y-%m-%d}" if updated else "wikidata-db"
            g = await asyncio.to_thread(Gazetteer, entries, load_knowledge(), version)
            log.info("gazetteer.loaded", entities=len(entries), surfaces=g.size, version=version)
        _cache = (now, sig, g)
        return g


async def store(
    conn: asyncpg.Connection, entities: list[WikiEntity], complete: bool
) -> dict[str, int]:
    """Upsert a refresh. Rows the refresh no longer returns are removed only when every
    query group answered (`complete`), so one timed-out group never empties a class."""
    have = int(await conn.fetchval("select count(*) from entity where source=$1", SOURCE) or 0)
    if not entities or len(entities) < have * MIN_KEEP_SHARE:
        log.warning("gazetteer.refresh_too_small", got=len(entities), have=have)
        return {"kept": have, "skipped": 1}
    started = datetime.now(UTC)
    rows = [
        (
            f"wikidata:{e.qid}",
            e.label,
            e.aliases,
            e.kind,
            e.desc,
            SOURCE,
            gazetteer.popularity(e.sitelinks),
            e.categories,
            e.sitelinks,
            started,
        )
        for e in entities
    ]
    async with conn.transaction():
        await conn.executemany(
            """insert into entity (id, label, aliases, kind, description, source, popularity,
                                   categories, sitelinks, updated_at)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               on conflict (id) do update set label=excluded.label, aliases=excluded.aliases,
                 kind=excluded.kind, description=excluded.description,
                 source=excluded.source, popularity=excluded.popularity,
                 categories=excluded.categories, sitelinks=excluded.sitelinks,
                 updated_at=excluded.updated_at""",
            rows,
        )
        removed = 0
        if complete:
            res = await conn.execute(
                "delete from entity where source=$1 and updated_at < $2", SOURCE, started
            )
            removed = int(res.split()[-1])
    return {"upserted": len(rows), "removed": removed}


def reset_cache() -> None:
    global _cache
    _cache = None
