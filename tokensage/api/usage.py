"""Per-key usage counters and daily quotas (guide §6.4). Persisted in api_usage so they
survive restarts; one upsert per request is cheap at this traffic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg


@dataclass
class Usage:
    requests: int = 0
    full_calls: int = 0
    refreshes: int = 0


async def bump(
    conn: asyncpg.Connection, key_name: str, *, requests: int = 0, full: int = 0, refresh: int = 0
) -> Usage:
    """Add to today's counters; returns the row as it stands after the update."""
    row = await conn.fetchrow(
        """insert into api_usage (key_name, day, requests, full_calls, refreshes)
           values ($1, (now() at time zone 'utc')::date, $2, $3, $4)
           on conflict (key_name, day) do update set
             requests = api_usage.requests + excluded.requests,
             full_calls = api_usage.full_calls + excluded.full_calls,
             refreshes = api_usage.refreshes + excluded.refreshes
           returning requests, full_calls, refreshes""",
        key_name,
        requests,
        full,
        refresh,
    )
    assert row is not None
    return Usage(row["requests"], row["full_calls"], row["refreshes"])


async def today(conn: asyncpg.Connection, key_name: str) -> Usage:
    row = await conn.fetchrow(
        """select requests, full_calls, refreshes from api_usage
           where key_name=$1 and day=(now() at time zone 'utc')::date""",
        key_name,
    )
    if not row:
        return Usage()
    return Usage(row["requests"], row["full_calls"], row["refreshes"])


async def all_today(conn: asyncpg.Connection) -> list[dict[str, int | str]]:
    rows = await conn.fetch(
        """select key_name, requests, full_calls, refreshes from api_usage
           where day=(now() at time zone 'utc')::date order by key_name"""
    )
    return [dict(r) for r in rows]


def seconds_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))
