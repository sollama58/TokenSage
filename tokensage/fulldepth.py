"""Full-depth helpers for the analyzer: cached X content, cached OCR, the trend index and
news confirmation (guide §4.3, §5.8, §6.3). All database-aware; the engine stays pure."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage.api.schemas import XInfo
from tokensage.config import Settings
from tokensage.engine import ocr, trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.sources import gnews
from tokensage.sources.x import ProfileData, TweetData, fetch_profile, fetch_tweet

log = structlog.get_logger("fulldepth")

PROFILE_TTL = timedelta(hours=12)
DELETED_RETRY = timedelta(hours=1)
NEWS_TTL = timedelta(hours=1)
TREND_INDEX_TTL_S = 600
MAX_NEWS_LOOKUPS = 2


# ----------------------------------------------------------------- X content with caching


async def tweet_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, tweet_id: str
) -> TweetData:
    row = await conn.fetchrow(
        "select first_snapshot, latest, status, source, fetched_at from x_tweet where tweet_id=$1",
        tweet_id,
    )
    if row:
        if row["status"] == "ok" and row["first_snapshot"]:
            # tweets are immutable apart from edits: the first-seen copy is the record
            return TweetData.from_json(row["first_snapshot"])
        if (
            row["status"] in ("deleted", "failed")
            and row["fetched_at"]
            and datetime.now(UTC) - row["fetched_at"] < DELETED_RETRY
        ):
            return TweetData(id=tweet_id, status=row["status"], source=row["source"])
    t = await fetch_tweet(
        http,
        tweet_id,
        paid_key=settings.twitterapi_io_key,
        allow_paid=settings.enable_paid_x,
    )
    snap = t.to_json()
    await conn.execute(
        """insert into x_tweet (tweet_id, first_snapshot, latest, status, source, fetched_at)
           values ($1, $2, $2, $3, $4, now())
           on conflict (tweet_id) do update set
             latest = excluded.latest,
             first_snapshot = case when x_tweet.status = 'ok' then x_tweet.first_snapshot
                                   else excluded.first_snapshot end,
             status = case when x_tweet.status = 'ok' and excluded.status <> 'ok'
                           then 'deleted' else excluded.status end,
             source = excluded.source, fetched_at = now()""",
        tweet_id,
        snap,
        t.status,
        t.source,
    )
    return t


async def profile_cached(
    conn: asyncpg.Connection, http: httpx.AsyncClient, handle: str
) -> ProfileData:
    row = await conn.fetchrow(
        """select user_id, handle, snapshot, source, fetched_at from x_profile
           where lower(handle) = lower($1) order by fetched_at desc limit 1""",
        handle,
    )
    if row and row["snapshot"] and row["fetched_at"]:
        if datetime.now(UTC) - row["fetched_at"] < PROFILE_TTL:
            return ProfileData.from_json(row["snapshot"])
    p = await fetch_profile(http, handle)
    if p.status == "failed":
        if row and row["snapshot"]:
            return ProfileData.from_json(row["snapshot"])  # stale beats nothing
        return p
    key = p.user_id or f"handle:{handle.lower()}"
    prev = await conn.fetchrow("select handle, snapshot from x_profile where user_id=$1", key)
    await conn.execute(
        """insert into x_profile (user_id, handle, snapshot, status, source, fetched_at)
           values ($1, $2, $3, $4, $5, now())
           on conflict (user_id) do update set handle=excluded.handle, snapshot=excluded.snapshot,
             status=excluded.status, source=excluded.source, fetched_at=now()""",
        key,
        p.handle,
        p.to_json(),
        p.status,
        p.source,
    )
    await conn.execute(
        "insert into x_profile_history (user_id, handle, followers) values ($1, $2, $3)",
        key,
        p.handle,
        p.followers,
    )
    if prev and prev["handle"] and p.handle and prev["handle"].lower() != p.handle.lower():
        p.username_changes = (p.username_changes or 0) + 1
        log.info("x.profile.renamed", user_id=key, old=prev["handle"], new=p.handle)
    return p


async def x_content(
    conn: asyncpg.Connection, http: httpx.AsyncClient, settings: Settings, x: XInfo | None
) -> tuple[TweetData | None, ProfileData | None]:
    if x is None:
        return None, None
    tweet: TweetData | None = None
    profile: ProfileData | None = None
    if x.ref.kind == "tweet" and x.ref.tweet_id:
        tweet = await tweet_cached(conn, http, settings, x.ref.tweet_id)
        if (
            tweet.status == "ok"
            and tweet.quoted is None
            and tweet.quoted_tweet_id
            and tweet.quoted_tweet_id != tweet.id
        ):
            # oEmbed and pre-quote cache rows carry only the quoted id: fetch it (cached too)
            tweet.quoted = await tweet_cached(conn, http, settings, tweet.quoted_tweet_id)
        if tweet.status == "ok" and tweet.author_handle:
            profile = await profile_cached(conn, http, tweet.author_handle)
    elif x.ref.kind == "profile" and x.ref.handle:
        profile = await profile_cached(conn, http, x.ref.handle)
    return tweet, profile


# ----------------------------------------------------------------- OCR cache


async def ocr_cached(conn: asyncpg.Connection, content_key: str | None) -> list[ocr.OcrLine] | None:
    if not content_key:
        return None
    row = await conn.fetchrow("select ocr, ocr_conf from image where content_key=$1", content_key)
    if not row or row["ocr"] is None:
        return None
    confs = list(row["ocr_conf"] or [])
    return [
        ocr.OcrLine(t, float(confs[i]) if i < len(confs) else 0.0)
        for i, t in enumerate(row["ocr"] or [])
    ]


async def persist_ocr(conn: asyncpg.Connection, content_key: str, lines: list[ocr.OcrLine]) -> None:
    await conn.execute(
        "update image set ocr=$2, ocr_conf=$3 where content_key=$1",
        content_key,
        [ln.text for ln in lines],
        [ln.confidence for ln in lines],
    )


# ----------------------------------------------------------------- trend index


_trend_cache: tuple[float, trends.TrendIndex] | None = None


async def trend_index(conn: asyncpg.Connection) -> trends.TrendIndex:
    global _trend_cache
    now = time.monotonic()
    if _trend_cache and now - _trend_cache[0] < TREND_INDEX_TTL_S:
        return _trend_cache[1]
    rows = await conn.fetch(
        """with latest as (select max(day) d from trend_term where source='wikipedia')
           select term, spike, views from trend_term, latest
           where source='wikipedia' and day >= latest.d - 2
             and (spike >= 2 or views >= 150000)
           order by spike desc nulls last limit 1500"""
    )
    terms = [
        trends.TrendTerm(r["term"], float(r["spike"] or 1.0), int(r["views"] or 0)) for r in rows
    ]
    idx = trends.TrendIndex(terms, load_knowledge())
    _trend_cache = (now, idx)
    return idx


# ----------------------------------------------------------------- news confirmation


async def news_for(
    conn: asyncpg.Connection, http: httpx.AsyncClient, term: str
) -> list[dict[str, Any]] | None:
    key = f"gnews:{term.lower()}"
    row = await conn.fetchrow("select value, fetched_at from lookup_cache where key=$1", key)
    if row and datetime.now(UTC) - row["fetched_at"] < NEWS_TTL:
        return list(row["value"])
    heads = await gnews.search(http, term)
    if heads is None:
        return list(row["value"]) if row else None
    value = [{"title": h.title, "source": h.source, "published": h.published} for h in heads]
    await conn.execute(
        """insert into lookup_cache (key, value, fetched_at) values ($1, $2, now())
           on conflict (key) do update set value=excluded.value, fetched_at=now()""",
        key,
        value,
    )
    return value
