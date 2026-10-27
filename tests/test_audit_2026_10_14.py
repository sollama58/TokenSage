"""Fixes from the 2026-10-14 audit: the HTTP client loggers stay quiet (the RPC URL carries
the Helius key), request bodies are capped, `tokens:now` respects the queue limit and a daily
budget, and the decision-time trend block is matched on the full read's inputs."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from tests.conftest import needs_db
from tests.fixtures.chain import SPL_MINT, T22_MINT
from tests.test_phase5_integration import make_client
from tokensage import now
from tokensage.api.app import MAX_BODY_BYTES
from tokensage.engine import trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.logging import configure_logging

# ----------------------------------------------------------------- H3: no URLs in the logs


@pytest.mark.parametrize("level", ["info", "debug"])
def test_http_client_loggers_stay_at_warning(level: str) -> None:
    configure_logging(level)
    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
        assert logging.getLogger(name).isEnabledFor(logging.WARNING)


# ----------------------------------------------------------------- M5: the full read's inputs


def _term(term: str, source: str = "x_trends") -> trends.TrendTerm:
    return trends.TrendTerm(
        term,
        0.0,
        3,
        source,
        seen_at=datetime.now(UTC) - timedelta(hours=1),
        rank=2,
        days_listed=0,
        direction="rising",
    )


def test_fresh_trend_reads_the_quoted_post() -> None:
    idx = trends.TrendIndex([_term("Moo Deng")], load_knowledge())
    doc: dict[str, Any] = {
        "raw": {"name": "Hippo Baby", "symbol": "HB"},
        "x": {"text": "launching now", "quoted": {"id": "1", "text": "look at Moo Deng go"}},
    }
    assert now.fresh_trend(doc, idx).matched
    doc["x"].pop("quoted")
    assert not now.fresh_trend(doc, idx).matched


def test_fresh_trend_keeps_the_full_reads_name_search_hits() -> None:
    idx = trends.TrendIndex([], load_knowledge())
    seen = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    doc = {
        "raw": {"name": "Glasnow", "symbol": "GLAS"},
        "trend": {
            "terms": [
                {"term": "Glasnow", "source": "news", "score": 0.5, "seen_at": seen},
                {"term": "Glasnow", "source": "bluesky", "posts": 12, "headline": "he did it"},
            ]
        },
    }
    t = now.fresh_trend(doc, idx)
    assert t.matched and {x.source for x in t.terms} == {"news", "bluesky"}
    by = {x.source: x for x in t.terms}
    assert by["news"].score == 0.5 and by["bluesky"].posts == 12
    assert by["bluesky"].headline == "he did it"


def test_fresh_trend_drops_name_search_hits_past_their_window() -> None:
    """Audit 2026-10-19: a refresh re-added a three-day-old headline as trending now."""
    idx = trends.TrendIndex([], load_knowledge())
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    doc: dict[str, Any] = {
        "raw": {"name": "Glasnow", "symbol": "GLAS"},
        "trend": {
            "terms": [
                {"term": "Glasnow", "source": "news", "score": 1.0, "seen_at": old},
                {"term": "Glasnow", "source": "bluesky", "posts": 40},
            ]
        },
        "analyzed_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
    }
    # the dateless Bluesky hit is dated by the read, 30 h ago: past Bluesky's 24 h
    assert not now.fresh_trend(doc, idx).matched
    doc["analyzed_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    t = now.fresh_trend(doc, idx)
    assert t.matched and {x.source for x in t.terms} == {"bluesky"}


def test_fresh_trend_matches_every_stored_referent() -> None:
    idx = trends.TrendIndex([_term("Peanut the Squirrel")], load_knowledge())
    doc = {
        "raw": {"name": "Nutty", "symbol": "NUT"},
        "referent": {"label": "Squirrel Day", "generic": False, "source": "entities:x"},
    }
    # the chosen referent alone does not trend
    assert not now.fresh_trend(doc, idx).matched
    refs = [
        {"label": "Squirrel Day", "kind": "event", "source": "entities:x", "score": 0.8},
        {"label": "Peanut the Squirrel", "kind": "famous_animal", "source": "x", "score": 0.6},
    ]
    t = now.fresh_trend(doc, idx, refs)
    assert t.matched and t.terms[0].term == "Peanut the Squirrel"
    assert t.terms[0].matched_on == "referent"


# ----------------------------------------------------------------- M8: body cap


@needs_db
async def test_request_bodies_over_the_cap_are_refused(migrated_db: str) -> None:
    big = json.dumps({"cas": ["x" * 200] * 6000})
    assert len(big) > MAX_BODY_BYTES
    async with make_client(migrated_db, inline_analyzer=False) as c:
        r = await c.post(
            "/v1/tokens:now", content=big, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

        async def chunks() -> Any:  # no Content-Length: counted as it arrives
            for i in range(0, len(big), 65536):
                yield big[i : i + 65536].encode()

        r = await c.post(
            "/v1/tokens:now", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 413
        # an over-long CA string is a validation error, not parsed further
        r = await c.post("/v1/tokens:now", json={"cas": ["x" * 300]})
        assert r.status_code == 422


# ----------------------------------------------------------------- M7: tokens:now limits


async def _full_reads(db: str, mints: list[str]) -> None:
    """Full reads with no refresh of their own: since rules 0.37.0 a full read the analyzer
    made stands as its mint's refresh (now.store_from_read), and an earlier test's read of the
    same mint would be served complete here instead of starting the job this test counts."""
    conn = await asyncpg.connect(db)
    try:
        await conn.execute(
            "delete from lookup_cache where key = any($1::text[])", [f"now:{m}" for m in mints]
        )
        for m in mints:
            await conn.execute(
                "insert into token (mint, created_at) values ($1, now()) on conflict do nothing",
                m,
            )
            await conn.execute(
                "insert into analysis (mint, version, depth, doc) values ($1, 1, 'full', $2)",
                m,
                json.dumps({"raw": {"name": "Moo Deng", "symbol": "MOO"}}),
            )
    finally:
        await conn.close()


async def _now_jobs(db: str) -> int:
    conn = await asyncpg.connect(db)
    try:
        return int(await conn.fetchval("select count(*) from job where kind='now'"))
    finally:
        await conn.close()


@needs_db
@pytest.mark.parametrize("limit", ["quota", "queue"])
async def test_tokens_now_respects_the_queue_and_the_daily_budget(
    migrated_db: str, clean_tables: None, limit: str
) -> None:
    await _full_reads(migrated_db, [SPL_MINT, T22_MINT])
    over = {"now_per_day": 1} if limit == "quota" else {"max_queue_depth": 1}
    code, status = ("quota_exceeded", 429) if limit == "quota" else ("overloaded", 503)
    async with make_client(migrated_db, inline_analyzer=False, **over) as c:
        # the first job starts; the second is refused, so the call still answers
        r = await c.post("/v1/tokens:now", json={"cas": [SPL_MINT, T22_MINT]})
        assert r.status_code == 200, r.text
        assert [i["status"] for i in r.json()["items"]] == ["pending", "pending"]
        assert await _now_jobs(migrated_db) == 1
        # joining the open job is free
        r = await c.post("/v1/tokens:now", json={"cas": [SPL_MINT]})
        assert r.status_code == 200
        # a call whose only new job is refused gets the error, and no job is left behind
        r = await c.post("/v1/tokens:now", json={"cas": [T22_MINT]})
        assert r.status_code == status and r.json()["error"]["code"] == code
        assert "retry-after" in r.headers
        assert await _now_jobs(migrated_db) == 1


def test_an_old_trend_index_reports_its_sources_stale() -> None:
    """Audit 2026-10-19: failing rebuilds kept serving hours-old lists as "ok"."""
    from tokensage import fulldepth

    idx = trends.TrendIndex(
        [],
        load_knowledge(),
        [trends.SourceStatus("x_trends", "ok"), trends.SourceStatus("wikipedia", "failed")],
    )
    assert fulldepth._aged(idx, fulldepth.TREND_INDEX_TTL_S) is idx
    old = fulldepth._aged(idx, fulldepth.TREND_INDEX_STALE_S + 60)
    assert [s.status for s in old.sources] == ["stale", "failed"]
    assert [s.status for s in idx.sources] == ["ok", "failed"]
