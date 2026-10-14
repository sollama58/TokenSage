"""Request-flow logic behind the routes (guide §6.2–6.3): cache check, enqueue, wait."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import asyncpg

from tokensage import queue
from tokensage.api import errors
from tokensage.api.auth import ApiKey
from tokensage.api.schemas import (
    Freshness,
    NowItem,
    TokenResponse,
    UpstreamError,
    stored_analysis,
)
from tokensage.api.usage import Usage
from tokensage.config import Settings
from tokensage.versions import NOW_KEY, NOW_USAGE_KEY, RULES_VERSION

DEPTH_RANK = {"basic": 0, "full": 1}
# A request that misses the cache while an identical job is committing reuses that job.
REUSE_DONE_WITHIN_S = 30.0
# A partial result (metadata pending, mint not yet on-chain) is re-analysed this soon.
PARTIAL_MAX_AGE_S = 60
# A failed analysis is reported as failed (no new job, no quota) for this long; a request
# with refresh=true tries again at once.
FAILED_COOLDOWN_S = 600
# token_not_found is the one failure expected to heal by itself within seconds (a mint
# seconds old is not visible on-chain yet; docs say "retry once after a few seconds"), so
# it is held for a much shorter time.
TOKEN_NOT_FOUND_COOLDOWN_S = 20
# hint fields that carry metadata: with any of them the analyzer never answers token_not_found
HINT_METADATA_FIELDS = (
    "name",
    "symbol",
    "description",
    "image_url",
    "twitter",
    "telegram",
    "website",
)
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


# Newest analysis at the requested depth or deeper, among the 5 newest versions ($2 is null
# for basic: any depth will do; 'full' otherwise). The depth filter runs in SQL so only the
# one document returned is detoasted, sent and decoded.
_LATEST_SQL = """select doc, version from (
      select doc, version, depth from analysis where mint=$1 order by version desc limit 5
    ) t where $2::text is null or depth = $2 order by version desc limit 1"""


def _min_depth(depth: str) -> str | None:
    return "full" if DEPTH_RANK[depth] >= DEPTH_RANK["full"] else None


async def latest_analysis(
    conn: asyncpg.Connection, mint: str, depth: str
) -> tuple[dict[str, Any], int] | None:
    """Newest analysis at the requested depth or deeper."""
    r = await conn.fetchrow(_LATEST_SQL, mint, _min_depth(depth))
    return (r["doc"], r["version"]) if r else None


async def _created_and_latest(
    conn: asyncpg.Connection, mint: str, depth: str
) -> tuple[datetime | None, tuple[dict[str, Any], int] | None]:
    """token.created_at and latest_analysis() in one round trip."""
    r = await conn.fetchrow(
        f"""select (select created_at from token where mint=$1) as created_at, a.doc, a.version
            from (select 1) one left join lateral ({_LATEST_SQL}) a on true""",
        mint,
        _min_depth(depth),
    )
    assert r is not None
    return r["created_at"], ((r["doc"], r["version"]) if r["version"] is not None else None)


async def _recent_failure(
    conn: asyncpg.Connection, mint: str, depth: str
) -> tuple[int, str | None, str | None] | None:
    """The newest analyze job for (mint, depth) when it failed within FAILED_COOLDOWN_S and
    nothing newer is open or done: (job_id, error_code, last_error)."""
    row = await conn.fetchrow(
        """select id, status, error_code, last_error, finished_at from job
           where kind='analyze' and mint=$1 and depth=$2 order by id desc limit 1""",
        mint,
        depth,
    )
    if not row or row["status"] != "failed" or row["finished_at"] is None:
        return None
    cooldown = (
        TOKEN_NOT_FOUND_COOLDOWN_S if row["error_code"] == "token_not_found" else FAILED_COOLDOWN_S
    )
    if (datetime.now(UTC) - row["finished_at"]).total_seconds() > cooldown:
        return None
    return row["id"], row["error_code"], row["last_error"]


def _failed_response(
    mint: str,
    depth: str,
    cached: tuple[dict[str, Any], int] | None,
    job_id: int,
    last_error: str | None,
    request_id: str,
) -> TokenResponse:
    """status=failed, the same shape whether the failure happened while this request waited
    or on an earlier one: the stale analysis (if any) and how the retry works."""
    return TokenResponse(
        ca=mint,
        status="failed",
        depth=depth,  # type: ignore[arg-type]
        stale_analysis=stored_analysis(cached[0]) if cached else None,
        errors=[
            UpstreamError(
                source="analyzer",
                code="failed",
                detail=(last_error or "analysis failed")
                + f"; retried automatically after {FAILED_COOLDOWN_S} s, or now "
                "with refresh=true",
            )
        ],
        job_id=job_id,
        request_id=request_id,
    )


def _freshness(doc: dict[str, Any], max_age_s: int, from_cache: bool) -> Freshness:
    analyzed_at = datetime.fromisoformat(doc["analyzed_at"])
    age = int((datetime.now(UTC) - analyzed_at).total_seconds())
    return Freshness(analyzed_at=analyzed_at, age_s=age, max_age_s=max_age_s, from_cache=from_cache)


def doc_errors(doc: dict[str, Any]) -> list[UpstreamError]:
    """The sources a stored read left out because the full-depth deadline passed (since
    rules 0.30.0): one `timeout` error each."""
    names = doc.get("timed_out") or []
    return [
        UpstreamError(
            source=str(n),
            code="timeout",
            detail="not finished within the full-depth deadline; left out of this read",
        )
        for n in names
        if isinstance(n, str)
    ]


def engagement_expired(
    doc: dict[str, Any], token_created_at: datetime | None, settings: Settings
) -> bool:
    """A young coin's cached read whose post counts (x.engagement.as_of) are older than
    X_ENGAGEMENT_REFRESH_S: not served from cache, so the read is re-run and the post
    fetched again. A read under X_ENGAGEMENT_RETRY_S old is still served (the re-fetch just
    failed, say: mirrors down), so a dead mirror does not re-run the read on every request.
    Coins older than X_ENGAGEMENT_YOUNG_S, or of unknown age, keep the usual max_age."""
    if token_created_at is None:
        return False
    now = datetime.now(UTC)
    if (now - token_created_at).total_seconds() >= settings.x_engagement_young_s:
        return False
    eng = (doc.get("x") or {}).get("engagement")
    if not isinstance(eng, dict):
        return False
    try:
        analyzed = datetime.fromisoformat(doc["analyzed_at"])
        if (now - analyzed).total_seconds() < settings.x_engagement_retry_s:
            return False
        as_of = datetime.fromisoformat(eng["as_of"]) if eng.get("as_of") else None
    except (KeyError, TypeError, ValueError):
        return False
    return as_of is None or (now - as_of).total_seconds() > settings.x_engagement_refresh_s


def _status_for(doc: dict[str, Any]) -> str:
    caveats = doc.get("caveats") or []
    return "partial" if any(str(c).startswith("partial:") for c in caveats) else "complete"


async def _enforce_quotas(conn: asyncpg.Connection, key: ApiKey, depth: str, refresh: bool) -> None:
    """Cache hits are free; a new full analysis or a forced refresh counts against the key.
    Runs inside the enqueue transaction; the per-key lock makes check-and-charge atomic, so
    concurrent requests for different coins cannot overspend the quota."""
    from tokensage.api import errors, usage

    await conn.execute("select pg_advisory_xact_lock(hashtext('quota:' || $1))", key.name)
    u = await usage.today(conn, key.name)
    if refresh and u.refreshes >= key.refresh_per_day:
        raise errors.quota_exceeded("refresh", usage.seconds_until_utc_midnight())
    if depth == "full" and u.full_calls >= key.full_per_day:
        raise errors.quota_exceeded("full-depth", usage.seconds_until_utc_midnight())
    await usage.bump(conn, key.name, full=1 if depth == "full" else 0, refresh=1 if refresh else 0)


async def quota_headers(
    pool: asyncpg.Pool, key: ApiKey, known: Usage | None = None
) -> dict[str, str]:
    """Today's remaining daily quotas for this key (UTC day), so callers can back off
    before a 429. `known` is a usage.Usage already read in this request when nothing has
    been charged since (it saves a round trip); otherwise the row is read again."""
    from tokensage.api import usage

    u = known
    if u is None:
        async with pool.acquire() as conn:
            u = await usage.today(conn, key.name)
    return {
        "X-Quota-Full-Remaining": str(max(0, key.full_per_day - u.full_calls)),
        "X-Quota-Refresh-Remaining": str(max(0, key.refresh_per_day - u.refreshes)),
    }


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
    hints: dict[str, Any] | None = None,
) -> TokenResponse:
    async with pool.acquire() as conn:
        created, cached = await _created_and_latest(conn, mint, depth)
        max_age = max_age_s if max_age_s is not None else default_max_age(settings, created)
        outdated = False
        if cached and not refresh:
            doc, _ = cached
            if max_age_s is None and _status_for(doc) == "partial":
                max_age = min(max_age, PARTIAL_MAX_AGE_S)
            fr = _freshness(doc, max_age, from_cache=True)
            # an analysis made by older rules (before a deploy) is re-run on the next
            # request instead of being served until it ages out; an explicit max_age wins
            outdated = max_age_s is None and (doc.get("versions") or {}).get("rules") != (
                RULES_VERSION
            )
            # a young coin's post counts are kept within X_ENGAGEMENT_REFRESH_S
            outdated = outdated or (
                max_age_s is None and engagement_expired(doc, created, settings)
            )
            if fr.age_s is not None and fr.age_s <= max_age and not outdated:
                return TokenResponse(
                    ca=mint,
                    status=_status_for(doc),  # type: ignore[arg-type]
                    depth=doc["depth"],
                    analysis=stored_analysis(doc),
                    freshness=fr,
                    errors=doc_errors(doc),
                    request_id=request_id,
                )

        # with metadata hints the analyzer never answers token_not_found (the mint is
        # analysed from the hints), so an earlier hint-less 404 says nothing about this request
        hinted = any(hints.get(f) for f in HINT_METADATA_FIELDS) if hints else False
        if not refresh:
            failed = await _recent_failure(conn, mint, depth)
            if failed is not None and not (hinted and failed[1] == "token_not_found"):
                fid, code, last_error = failed
                if code in DEFINITIVE_CODES:
                    from tokensage.api import errors

                    status, api_code = DEFINITIVE_CODES[code]
                    raise errors.ApiError(status, api_code, last_error or api_code)
                return _failed_response(mint, depth, cached, fid, last_error, request_id)

        async def charge(j: queue.Job) -> None:
            # Only a newly created job costs quota or counts against the queue limit.
            # Joining an open job (the documented 202 -> retry loop) or reusing one that
            # just finished is always allowed. Raising here rolls the new job back.
            if not j.inserted:
                return
            if await queue.pending_count(conn) > settings.max_queue_depth:
                from tokensage.api import errors

                raise errors.overloaded()
            if key is not None:
                await _enforce_quotas(conn, key, depth, refresh)

        async def enqueue(reuse_s: float) -> queue.Job:
            return await queue.enqueue(
                conn,
                "analyze",
                mint,
                depth,
                priority=priority,
                requested_by=requested_by,
                reuse_done_within_s=reuse_s,
                before_commit=charge,
                payload={"hints": hints} if hints else None,
            )

        # a refresh always re-analyses; otherwise reuse a job that just finished
        # (nor one whose result came from older rules)
        job = await enqueue(0 if refresh or outdated else REUSE_DONE_WITHIN_S)
        reused = job.status == "done"
        if reused:
            # The reused job's result must satisfy the caller's bound: it closes the race of
            # a caller that missed the cache while the job committed, but it must not hand
            # a short (or zero) max_age the document that bound already rejected.
            fresh = await latest_analysis(conn, mint, depth)
            if fresh is not None:
                doc, _ = fresh
                fr = _freshness(doc, max_age, from_cache=True)
                if fr.age_s is not None and fr.age_s <= max_age:
                    return TokenResponse(
                        ca=mint,
                        status=_status_for(doc),  # type: ignore[arg-type]
                        depth=doc["depth"],
                        analysis=stored_analysis(doc),
                        freshness=fr,
                        errors=doc_errors(doc),
                        request_id=request_id,
                    )
            job = await enqueue(0)
        if callback_url and key is not None:
            from tokensage import callbacks

            await callbacks.schedule(
                conn, target_job_id=job.id, callback_url=callback_url, key_digest=key.digest
            )

    if job.status == "done":
        finished = True
    else:
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
                        analysis=stored_analysis(doc),
                        freshness=_freshness(doc, max_age, from_cache=False),
                        errors=doc_errors(doc),
                        request_id=request_id,
                    )
            if j and j.status == "failed" and j.error_code in DEFINITIVE_CODES:
                from tokensage.api import errors

                status, code = DEFINITIVE_CODES[j.error_code]
                raise errors.ApiError(status, code, j.last_error or code)
            if j and j.status == "failed":
                return _failed_response(mint, depth, cached, job.id, j.last_error, request_id)
        stale = cached[0] if cached else None
        return TokenResponse(
            ca=mint,
            status="pending",
            depth=depth,  # type: ignore[arg-type]
            stale_analysis=stored_analysis(stale) if stale else None,
            freshness=_freshness(stale, max_age, from_cache=True) if stale else Freshness(),
            job_id=job.id,
            request_id=request_id,
        )


# ----------------------------------------------------------------- decision-time refresh


async def now_items(
    pool: asyncpg.Pool,
    waiter: queue.DoneWaiter,
    settings: Settings,
    mints: list[str],
    wait_s: int,
    requested_by: str,
) -> dict[str, NowItem]:
    """The stored refresh per mint (POST /v1/tokens:now), starting a `now` job for each one
    older than NOW_MAX_AGE_S or made by older rules. Free of the read quotas, but each new job
    counts against the key's NOW_PER_DAY and the queue limit; when the first job of the call
    is refused the call answers 429 / 503, and a later refusal leaves the rest unstarted. A
    mint with no full read is `unavailable` without a job."""

    async def stored(conn: asyncpg.Connection) -> dict[str, tuple[dict[str, Any], datetime]]:
        rows = await conn.fetch(
            "select key, value, fetched_at from lookup_cache where key = any($1::text[])",
            [NOW_KEY + m for m in mints],
        )
        return {
            r["key"].removeprefix(NOW_KEY): (r["value"], r["fetched_at"])
            for r in rows
            if isinstance(r["value"], dict)
        }

    def fresh(v: tuple[dict[str, Any], datetime] | None) -> bool:
        return (
            v is not None
            and v[0].get("status") == "complete"
            and v[0].get("rules") == RULES_VERSION
            and (datetime.now(UTC) - v[1]).total_seconds() <= settings.now_max_age_s
        )

    jobs: list[int] = []
    async with pool.acquire() as conn:
        have = await stored(conn)
        stale = [m for m in mints if not fresh(have.get(m))]
        full = {
            r["mint"]
            for r in await conn.fetch(
                "select distinct mint from analysis where mint = any($1::text[]) "
                "and depth = 'full'",
                stale,
            )
        }

        async def charge(j: queue.Job) -> None:
            # as get_or_enqueue: only a new job counts against the queue limit and the key's
            # daily `now` budget (audit 2026-10-14 M7); joining an open one is always allowed
            from tokensage.api import usage

            if not j.inserted:
                return
            if await queue.pending_count(conn) > settings.max_queue_depth:
                raise errors.overloaded()
            used = await usage.bump(conn, NOW_USAGE_KEY + requested_by, requests=1)
            if used.requests > settings.now_per_day:
                raise errors.quota_exceeded("now", usage.seconds_until_utc_midnight())

        refused: Exception | None = None
        for m in stale:
            if m not in full:
                continue
            try:
                job = await queue.enqueue(
                    conn,
                    "now",
                    m,
                    "full",
                    priority=queue.PRIORITY_API,
                    requested_by=requested_by,
                    reuse_done_within_s=min(30, settings.now_max_age_s),
                    before_commit=charge,
                )
            except errors.ApiError as e:
                # the refused job is rolled back; the ones already started run
                refused = e
                break
            if job.status != "done":
                jobs.append(job.id)
        if refused is not None and not jobs:
            raise refused
    if jobs and wait_s > 0:
        await asyncio.gather(*(waiter.wait(j, wait_s) for j in jobs))
    async with pool.acquire() as conn:
        have = await stored(conn)
    out: dict[str, NowItem] = {}
    for m in mints:
        v = have.get(m)
        if fresh(v):
            assert v is not None
            body = {k: val for k, val in v[0].items() if k != "rules"}
            out[m] = NowItem.model_validate(body)
        elif m not in full:
            out[m] = NowItem(ca=m, status="unavailable", error="no_full_read")
        elif v is not None and v[0].get("status") == "complete":
            body = {k: val for k, val in v[0].items() if k != "rules"}
            out[m] = NowItem.model_validate({**body, "status": "pending"})
        else:
            out[m] = NowItem(ca=m, status="pending")
    return out
