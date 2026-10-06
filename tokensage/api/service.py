"""Request-flow logic behind the routes (guide §6.2–6.3): cache check, enqueue, wait."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

from tokensage import queue
from tokensage.api.auth import ApiKey
from tokensage.api.schemas import Analysis, Freshness, TokenResponse, UpstreamError
from tokensage.config import Settings

DEPTH_RANK = {"basic": 0, "full": 1}
# analyzer error_code -> (http status, api code)
DEFINITIVE_CODES = {
    "token_not_found": (404, "token_not_found"),
    "not_a_token_mint": (422, "not_a_token_mint"),
    "not_pumpfun": (422, "not_pumpfun"),
}


def default_max_age(settings: Settings, token_created_at: datetime | None) -> int:
    if token_created_at is None:
        return settings.max_age_mid_s
    age = (datetime.now(UTC) - token_created_at).total_seconds()
    if age < 3600:
        return settings.max_age_young_s
    if age < 7 * 86400:
        return settings.max_age_mid_s
    return settings.max_age_old_s


async def latest_analysis(
    conn: asyncpg.Connection, mint: str, depth: str
) -> tuple[dict[str, Any], int] | None:
    """Newest analysis at the requested depth or deeper."""
    rows = await conn.fetch(
        "select doc, version, depth from analysis where mint=$1 order by version desc limit 5", mint
    )
    for r in rows:
        if DEPTH_RANK.get(r["depth"], 0) >= DEPTH_RANK[depth]:
            return r["doc"], r["version"]
    return None


async def token_created_at(conn: asyncpg.Connection, mint: str) -> datetime | None:
    return await conn.fetchval("select created_at from token where mint=$1", mint)


def _freshness(doc: dict[str, Any], max_age_s: int, from_cache: bool) -> Freshness:
    analyzed_at = datetime.fromisoformat(doc["analyzed_at"])
    age = int((datetime.now(UTC) - analyzed_at).total_seconds())
    return Freshness(analyzed_at=analyzed_at, age_s=age, max_age_s=max_age_s, from_cache=from_cache)


def _status_for(doc: dict[str, Any]) -> str:
    caveats = doc.get("caveats") or []
    return "partial" if any(str(c).startswith("partial:") for c in caveats) else "complete"


async def _enforce_quotas(conn: asyncpg.Connection, key: ApiKey, depth: str, refresh: bool) -> None:
    """Cache hits are free; a new full analysis or a forced refresh counts against the key."""
    from tokensage.api import errors, usage

    u = await usage.today(conn, key.name)
    if refresh and u.refreshes >= key.refresh_per_day:
        raise errors.quota_exceeded("refresh", usage.seconds_until_utc_midnight())
    if depth == "full" and u.full_calls >= key.full_per_day:
        raise errors.quota_exceeded("full-depth", usage.seconds_until_utc_midnight())
    await usage.bump(conn, key.name, full=1 if depth == "full" else 0, refresh=1 if refresh else 0)


async def get_or_enqueue(
    pool: asyncpg.Pool,
    waiter: queue.DoneWaiter,
    settings: Settings,
    *,
    mint: str,
    depth: str,
    wait_s: int,
    max_age_s: int | None,
    refresh: bool,
    requested_by: str,
    request_id: str,
    priority: int = queue.PRIORITY_API,
    key: ApiKey | None = None,
    callback_url: str | None = None,
) -> TokenResponse:
    async with pool.acquire() as conn:
        created = await token_created_at(conn, mint)
        max_age = max_age_s if max_age_s is not None else default_max_age(settings, created)
        cached = await latest_analysis(conn, mint, depth)
        if cached and not refresh:
            doc, _ = cached
            fr = _freshness(doc, max_age, from_cache=True)
            if fr.age_s is not None and fr.age_s <= max_age:
                return TokenResponse(
                    ca=mint,
                    status=_status_for(doc),  # type: ignore[arg-type]
                    depth=doc["depth"],
                    analysis=Analysis.model_validate(doc),
                    freshness=fr,
                    request_id=request_id,
                )
        if await queue.pending_count(conn) >= settings.max_queue_depth:
            from tokensage.api import errors

            raise errors.overloaded()
        if key is not None:
            await _enforce_quotas(conn, key, depth, refresh)
        job = await queue.enqueue(
            conn, "analyze", mint, depth, priority=priority, requested_by=requested_by
        )
        if callback_url and key is not None:
            from tokensage import callbacks

            await callbacks.schedule(
                conn, target_job_id=job.id, callback_url=callback_url, key_digest=key.digest
            )

    finished = await waiter.wait(job.id, wait_s) if wait_s > 0 else False

    async with pool.acquire() as conn:
        if finished:
            j = await queue.get(conn, job.id)
            if j and j.status == "done":
                fresh = await latest_analysis(conn, mint, depth)
                if fresh:
                    doc, _ = fresh
                    return TokenResponse(
                        ca=mint,
                        status=_status_for(doc),  # type: ignore[arg-type]
                        depth=doc["depth"],
                        analysis=Analysis.model_validate(doc),
                        freshness=_freshness(doc, max_age, from_cache=False),
                        request_id=request_id,
                    )
            if j and j.status == "failed" and j.error_code in DEFINITIVE_CODES:
                from tokensage.api import errors

                status, code = DEFINITIVE_CODES[j.error_code]
                raise errors.ApiError(status, code, j.last_error or code)
            if j and j.status == "failed":
                return TokenResponse(
                    ca=mint,
                    status="failed",
                    depth=depth,  # type: ignore[arg-type]
                    errors=[UpstreamError(source="analyzer", code="failed", detail=j.last_error)],
                    job_id=job.id,
                    request_id=request_id,
                )
        stale = cached[0] if cached else None
        return TokenResponse(
            ca=mint,
            status="pending",
            depth=depth,  # type: ignore[arg-type]
            stale_analysis=Analysis.model_validate(stale) if stale else None,
            freshness=_freshness(stale, max_age, from_cache=True) if stale else Freshness(),
            job_id=job.id,
            request_id=request_id,
        )
