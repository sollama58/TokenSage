"""The admin API (/admin/v1): manage consumer API keys, read usage, queue and source health,
and retry failed jobs. Every route needs the ADMIN_KEY as a bearer token.

Keys created here live in the api_key table; the raw key is returned once, at creation or
rotation, and only its SHA-256 is stored. Keys from the API_KEYS env var are listed too but
are read-only (change them in the environment)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from tokensage import __version__, queue, recall
from tokensage.api import errors, usage
from tokensage.api.auth import ApiKey, KeyStore, new_raw_key, require_admin, sha256_hex
from tokensage.api.schemas import Depth, _Model
from tokensage.versions import LEXICON_VERSION, RULES_VERSION

log = structlog.get_logger("admin")
router = APIRouter(prefix="/admin/v1", tags=["admin"], dependencies=[Depends(require_admin)])

KeyName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Limit = Annotated[int, Field(ge=1, le=10_000_000)]
ADMIN_RESPONSES: dict[int | str, dict] = {403: {"description": "forbidden (admin key required)"}}


# ---------------------------------------------------------------- models


class UsageDay(_Model):
    key_name: str
    day: date
    requests: int
    full_calls: int
    refreshes: int


class UsageToday(_Model):
    requests: int = 0
    full_calls: int = 0
    refreshes: int = 0


class KeyInfo(_Model):
    name: str
    source: Literal["env", "db"]
    rate_per_min: int
    full_per_day: int
    refresh_per_day: int
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    usage_today: UsageToday = UsageToday()


class KeyList(_Model):
    keys: list[KeyInfo]


class KeyCreate(_Model):
    name: KeyName
    rate_per_min: Limit | None = None
    full_per_day: Limit | None = None
    refresh_per_day: Limit | None = None


class KeyUpdate(_Model):
    rate_per_min: Limit | None = None
    full_per_day: Limit | None = None
    refresh_per_day: Limit | None = None


class KeySecret(KeyInfo):
    key: str = Field(description="The raw API key. Shown only once; store it now.")


class UsageResponse(_Model):
    days: int
    rows: list[UsageDay]


class QueueStats(_Model):
    pending: int
    running: int
    failed_24h: int
    done_24h: int
    oldest_pending_s: float | None


class SourceHealth(_Model):
    source: str
    state: str | None
    failures: int
    open_until: datetime | None
    calls_today: int
    spend_today_usd: float


class StatusResponse(_Model):
    status: Literal["ok"] = "ok"
    service_version: str
    rules_version: str
    lexicon_version: str
    default_depth: str
    inline_analyzer: bool
    queue: QueueStats
    sources: list[SourceHealth]
    usage_today: list[UsageDay]
    recall_24h: dict


class AdminJob(_Model):
    id: int
    kind: str
    mint: str | None
    depth: str | None
    status: str
    priority: int
    attempts: int
    error_code: str | None
    last_error: str | None
    requested_by: str | None
    result_version: int | None
    created_at: datetime
    finished_at: datetime | None


class JobList(_Model):
    jobs: list[AdminJob]


# ---------------------------------------------------------------- helpers


def _store(request: Request) -> KeyStore:
    return request.app.state.keys


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _env_key(store: KeyStore, name: str) -> ApiKey | None:
    return next((k for k in store.env_keys if k.name == name), None)


async def _usage_map(conn: asyncpg.Connection) -> dict[str, UsageToday]:
    return {
        str(u["key_name"]): UsageToday(
            requests=int(u["requests"]),
            full_calls=int(u["full_calls"]),
            refreshes=int(u["refreshes"]),
        )
        for u in await usage.all_today(conn)
    }


def _db_key_info(r: asyncpg.Record, use: dict[str, UsageToday]) -> KeyInfo:
    return KeyInfo(
        name=r["name"],
        source="db",
        rate_per_min=r["rate_per_min"],
        full_per_day=r["full_per_day"],
        refresh_per_day=r["refresh_per_day"],
        created_at=r["created_at"],
        revoked_at=r["revoked_at"],
        usage_today=use.get(r["name"], UsageToday()),
    )


def _read_only(name: str) -> errors.ApiError:
    return errors.ApiError(
        409, "read_only", f"key '{name}' comes from the API_KEYS env var; change it there"
    )


def _no_key(name: str) -> errors.ApiError:
    return errors.not_found("key_not_found", f"no key named '{name}'")


KEY_COLS = "name, rate_per_min, full_per_day, refresh_per_day, created_at, revoked_at"


# ---------------------------------------------------------------- status


@router.get("/status", response_model=StatusResponse, responses=ADMIN_RESPONSES)
async def status(request: Request, response: Response) -> StatusResponse:
    """Queue depth, upstream source health, today's usage per key and the last day's
    referent recall."""
    settings = request.app.state.settings
    async with _pool(request).acquire() as conn:
        q = await conn.fetchrow(
            """select
                 count(*) filter (where status='pending' and run_after <= now()) as pending,
                 count(*) filter (where status='running') as running,
                 count(*) filter (where status='failed'
                                  and finished_at > now() - interval '1 day') as failed_24h,
                 count(*) filter (where status='done'
                                  and finished_at > now() - interval '1 day') as done_24h,
                 extract(epoch from now() - min(greatest(created_at, run_after))
                                             filter (where status='pending'
                                                     and run_after <= now()))
                   as oldest_pending_s
               from job"""
        )
        sources = await conn.fetch("select * from source_health order by source")
        use = await conn.fetch(
            """select key_name, day, requests, full_calls, refreshes from api_usage
               where day=(now() at time zone 'utc')::date order by key_name"""
        )
        rec = await recall.summary(conn, 24)
    response.headers["Cache-Control"] = "no-store"
    return StatusResponse(
        service_version=__version__,
        rules_version=RULES_VERSION,
        lexicon_version=LEXICON_VERSION,
        default_depth=settings.default_depth,
        inline_analyzer=settings.inline_analyzer,
        queue=QueueStats(
            pending=q["pending"],
            running=q["running"],
            failed_24h=q["failed_24h"],
            done_24h=q["done_24h"],
            oldest_pending_s=(
                float(q["oldest_pending_s"]) if q["oldest_pending_s"] is not None else None
            ),
        ),
        sources=[
            SourceHealth(
                source=s["source"],
                state=s["state"],
                failures=s["failures"],
                open_until=s["open_until"],
                calls_today=s["calls_today"],
                spend_today_usd=float(s["spend_today_usd"]),
            )
            for s in sources
        ],
        usage_today=[UsageDay(**dict(u)) for u in use],
        recall_24h=rec,
    )


# ---------------------------------------------------------------- keys


@router.get("/keys", response_model=KeyList, responses=ADMIN_RESPONSES)
async def list_keys(request: Request, include_revoked: bool = False) -> KeyList:
    """Every consumer key (env and managed) with its limits and today's usage."""
    store = _store(request)
    async with _pool(request).acquire() as conn:
        use = await _usage_map(conn)
        rows = await conn.fetch(
            f"select {KEY_COLS} from api_key where $1 or revoked_at is null order by name",
            include_revoked,
        )
    env = [
        KeyInfo(
            name=k.name,
            source="env",
            rate_per_min=k.rate_per_min,
            full_per_day=k.full_per_day,
            refresh_per_day=k.refresh_per_day,
            usage_today=use.get(k.name, UsageToday()),
        )
        for k in sorted(store.env_keys, key=lambda k: k.name)
    ]
    env_names = {k.name for k in env}
    return KeyList(keys=env + [_db_key_info(r, use) for r in rows if r["name"] not in env_names])


@router.post(
    "/keys",
    response_model=KeySecret,
    status_code=201,
    responses={**ADMIN_RESPONSES, 409: {"description": "key_exists"}},
)
async def create_key(request: Request, response: Response, body: KeyCreate) -> KeySecret:
    """Create a consumer key. Limits default to the service defaults. The raw key is in the
    response and is never shown again. Names are never reused, even after a revoke, so
    usage history stays attributable."""
    settings = request.app.state.settings
    store = _store(request)
    if _env_key(store, body.name):
        raise errors.ApiError(409, "key_exists", f"a key named '{body.name}' already exists")
    raw = new_raw_key()
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            f"""insert into api_key (name, key_sha256, rate_per_min, full_per_day,
                                     refresh_per_day)
                values ($1, $2, $3, $4, $5)
                on conflict (name) do nothing
                returning {KEY_COLS}""",
            body.name,
            sha256_hex(raw),
            body.rate_per_min or settings.rate_per_min_default,
            body.full_per_day or settings.full_per_day_default,
            body.refresh_per_day or settings.refresh_per_day_default,
        )
        if row is None:
            raise errors.ApiError(409, "key_exists", f"a key named '{body.name}' already exists")
        await store.reload(conn)
    log.info("admin.key_created", key=body.name)
    response.headers["Cache-Control"] = "no-store"
    return KeySecret(**_db_key_info(row, {}).model_dump(), key=raw)


@router.patch(
    "/keys/{name}",
    response_model=KeyInfo,
    responses={
        **ADMIN_RESPONSES,
        404: {"description": "key_not_found"},
        409: {"description": "read_only"},
    },
)
async def update_key(request: Request, name: str, body: KeyUpdate) -> KeyInfo:
    """Change a managed key's limits. Takes effect on the key's next request, with a full
    rate bucket."""
    store = _store(request)
    if _env_key(store, name):
        raise _read_only(name)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            f"""update api_key set
                  rate_per_min = coalesce($2, rate_per_min),
                  full_per_day = coalesce($3, full_per_day),
                  refresh_per_day = coalesce($4, refresh_per_day)
                where name = $1 and revoked_at is null
                returning {KEY_COLS}""",
            name,
            body.rate_per_min,
            body.full_per_day,
            body.refresh_per_day,
        )
        if row is None:
            raise _no_key(name)
        await store.reload(conn)
        use = await _usage_map(conn)
    store.limiter.buckets.pop(name, None)  # start the new rate with a full bucket
    log.info("admin.key_updated", key=name, **body.model_dump(exclude_none=True))
    return _db_key_info(row, use)


@router.post(
    "/keys/{name}/rotate",
    response_model=KeySecret,
    responses={
        **ADMIN_RESPONSES,
        404: {"description": "key_not_found"},
        409: {"description": "read_only"},
    },
)
async def rotate_key(request: Request, response: Response, name: str) -> KeySecret:
    """Replace a managed key's secret. The old key stops working at once; limits and usage
    carry over. Callbacks for jobs enqueued before the rotation stay signed with the old
    key's digest."""
    store = _store(request)
    if _env_key(store, name):
        raise _read_only(name)
    raw = new_raw_key()
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            f"""update api_key set key_sha256 = $2
                where name = $1 and revoked_at is null
                returning {KEY_COLS}""",
            name,
            sha256_hex(raw),
        )
        if row is None:
            raise _no_key(name)
        await store.reload(conn)
        use = await _usage_map(conn)
    log.info("admin.key_rotated", key=name)
    response.headers["Cache-Control"] = "no-store"
    return KeySecret(**_db_key_info(row, use).model_dump(), key=raw)


@router.delete(
    "/keys/{name}",
    response_model=KeyInfo,
    responses={
        **ADMIN_RESPONSES,
        404: {"description": "key_not_found"},
        409: {"description": "read_only"},
    },
)
async def revoke_key(request: Request, name: str) -> KeyInfo:
    """Revoke a managed key. It stops working at once; its usage history is kept."""
    store = _store(request)
    if _env_key(store, name):
        raise _read_only(name)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            f"""update api_key set revoked_at = now()
                where name = $1 and revoked_at is null
                returning {KEY_COLS}""",
            name,
        )
        if row is None:
            raise _no_key(name)
        await store.reload(conn)
    log.info("admin.key_revoked", key=name)
    return _db_key_info(row, {})


# ---------------------------------------------------------------- usage


@router.get("/usage", response_model=UsageResponse, responses=ADMIN_RESPONSES)
async def get_usage(
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    key: str | None = None,
) -> UsageResponse:
    """Daily request, full-depth and refresh counts per key (UTC days, newest first)."""
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """select key_name, day, requests, full_calls, refreshes from api_usage
               where day > (now() at time zone 'utc')::date - $1::int
                 and ($2::text is null or key_name = $2)
               order by day desc, key_name""",
            days,
            key,
        )
    return UsageResponse(days=days, rows=[UsageDay(**dict(r)) for r in rows])


# ---------------------------------------------------------------- jobs

JOB_COLS = """id, kind, mint, depth, status, priority, attempts, error_code, last_error,
              requested_by, result_version, created_at, finished_at"""


@router.get("/jobs", response_model=JobList, responses=ADMIN_RESPONSES)
async def list_jobs(
    request: Request,
    status: Literal["pending", "running", "done", "failed"] | None = None,
    mint: str | None = None,
    depth: Depth | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobList:
    """Recent jobs, newest first."""
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            f"""select {JOB_COLS} from job
                where ($1::text is null or status = $1)
                  and ($2::text is null or mint = $2)
                  and ($3::text is null or depth = $3)
                order by id desc limit $4""",
            status,
            mint,
            depth,
            limit,
        )
    return JobList(jobs=[AdminJob(**dict(r)) for r in rows])


@router.post(
    "/jobs/{job_id}/retry",
    response_model=AdminJob,
    responses={
        **ADMIN_RESPONSES,
        404: {"description": "job_not_found"},
        409: {"description": "job_not_failed"},
    },
)
async def retry_job(request: Request, job_id: int) -> AdminJob:
    """Enqueue a failed job again (same kind, mint, depth and hints). Returns the new job,
    or the already-open one for the same token and depth."""
    async with _pool(request).acquire() as conn:
        old = await conn.fetchrow(
            "select kind, mint, depth, status, payload from job where id=$1", job_id
        )
        if old is None:
            raise errors.not_found("job_not_found", f"no job {job_id}")
        if old["status"] != "failed":
            raise errors.ApiError(
                409, "job_not_failed", f"job {job_id} is {old['status']}; only failed jobs retry"
            )
        job = await queue.enqueue(
            conn,
            old["kind"],
            old["mint"],
            old["depth"],
            priority=queue.PRIORITY_API,
            requested_by="admin",
            payload=old["payload"],
        )
        row = await conn.fetchrow(f"select {JOB_COLS} from job where id=$1", job.id)
    log.info("admin.job_retried", job_id=job_id, new_job_id=job.id)
    return AdminJob(**dict(row))


# ---------------------------------------------------------------- recall


@router.get("/recall", responses=ADMIN_RESPONSES)
async def get_recall(request: Request, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24) -> dict:
    """How often the engine resolved what a coin refers to, per depth, over `hours`."""
    async with _pool(request).acquire() as conn:
        return await recall.summary(conn, hours)
