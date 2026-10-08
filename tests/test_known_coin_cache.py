"""The analyzer's known_coin cache: rebuilt only when the table changes, and every change
(new coin, re-categorised coin, new logo hash, deletion) is seen by the next read."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tokensage import analyzer
from tokensage.engine import known_coins
from tokensage.engine.knowledge import KnownCoin, load_knowledge


@pytest.fixture
async def db(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_db)
    await conn.execute("delete from known_coin")
    analyzer._known_cache = None
    yield conn
    await conn.execute("delete from known_coin")
    analyzer._known_cache = None
    await conn.close()


async def _insert(db: asyncpg.Connection, cid: str, symbol: str, name: str) -> None:
    await db.execute(
        """insert into known_coin (id, chain, symbol, name, aliases, lore, categories, source,
                                   updated_at)
           values ($1, 'any', $2, $3, '{}', 'lore', '{meme}', 'coingecko', now())""",
        cid,
        symbol,
        name,
    )


async def test_known_coins_cached_until_the_table_changes(db: asyncpg.Connection) -> None:
    assert await analyzer._known_coins(db) == ([], [])
    await _insert(db, "coingecko:a", "AAA", "Alpha Coin")
    coins, logos = await analyzer._known_coins(db)
    assert [c.symbol for c in coins] == ["AAA"] and logos == []
    again = await analyzer._known_coins(db)
    assert again[0] is coins  # unchanged table: the same objects, nothing rebuilt
    # a logo hash written without touching updated_at is still picked up
    await db.execute("update known_coin set logo_phash=42 where id='coingecko:a'")
    coins, logos = await analyzer._known_coins(db)
    assert [(c.known_coin, c.phash) for c in logos] == [("AAA", 42)]
    await db.execute("update known_coin set name='Alpha Two' where id='coingecko:a'")
    coins, _ = await analyzer._known_coins(db)
    assert [c.name for c in coins] == ["Alpha Two"]
    await db.execute("delete from known_coin")
    assert await analyzer._known_coins(db) == ([], [])


def test_compact_surfaces_match_compact() -> None:
    coin = KnownCoin(
        symbol="X",
        name="Dog-Wif Hat",
        aliases=("Ünï Côin", "DOG WIF", "dog-wif hat"),
        chain="solana",
        lore="",
        categories=(),
        referent_label="",
        referent_kind="coin",
        referent_desc="",
    )
    for c in [coin, *load_knowledge().coins]:
        assert c.compact_surfaces == tuple((s, known_coins._compact(s)) for s in c.surfaces)
