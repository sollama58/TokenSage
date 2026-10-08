"""Monitoring reads for the admin panel (/admin): Helius credits against the plan, every
upstream's calls, errors and latency, queue and job timing, and the freshness of the
knowledge the engine leans on. Same ADMIN_KEY bearer as the rest of /admin/v1.

Upstream numbers come from upstream_usage, which every process adds into every 30 s (see
net/metrics.py), so they trail live traffic by up to that long."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from tokensage.api.auth import require_admin
from tokensage.api.routes_admin import ADMIN_RESPONSES
from tokensage.api.schemas import _Model
from tokensage.net import metrics
from tokensage.versions import PAID_X_USAGE_KEY

router = APIRouter(prefix="/admin/v1", tags=["admin"], dependencies=[Depends(require_admin)])

RPC = "solana_rpc"


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _f(v: object) -> float | None:
    return None if v is None else round(float(v), 3)  # type: ignore[arg-type]


def _jsonb(v: object) -> dict:
    """jsonb arrives decoded on the app pool (db.create_pool sets a codec), as text without."""
    return json.loads(v) if isinstance(v, str) else dict(v or {})  # type: ignore[call-overload]


def billing_cycle(now: datetime, billing_day: int) -> tuple[datetime, datetime]:
    """The Helius credit cycle around `now`: from the last `billing_day` (UTC midnight) to
    the same day next month."""
    day = max(1, min(28, billing_day))

    def at(y: int, m: int) -> datetime:
        return datetime(y, m, day, tzinfo=UTC)

    start = at(now.year, now.month)
    if start > now:
        start = at(now.year - 1, 12) if now.month == 1 else at(now.year, now.month - 1)
    end = at(start.year + 1, 1) if start.month == 12 else at(start.year, start.month + 1)
    return start, end


# ---------------------------------------------------------------- models


class HeliusPlan(_Model):
    credits_per_cycle: int
    rps_limit: int
    billing_day: int
    cycle_start: datetime
    cycle_end: datetime


class HeliusCredits(_Model):
    this_hour: int
    last_hour: int
    today: int
    last_24h: int
    last_7d: int
    cycle_to_date: int
    cycle_pct: float
    per_day_now: float = Field(description="Credits per day at the last 24 hours' pace")
    projected_cycle: int
    projected_pct: float
    days_left_in_cycle: float
    days_until_exhausted: float | None


class HeliusCalls(_Model):
    today: int
    errors_today: int
    rate_limited_today: int
    last_24h: int
    errors_24h: int
    rate_limited_24h: int
    avg_ms_24h: float | None
    peak_rps_24h: int


class HeliusMethod(_Model):
    method: str
    credit_cost: int
    calls_today: int
    credits_today: int
    errors_today: int
    calls_24h: int
    credits_24h: int
    avg_ms_24h: float | None
    calls_cycle: int
    credits_cycle: int


class HourPoint(_Model):
    hour: datetime
    calls: int
    credits: int
    errors: int
    rate_limited: int
    peak_rps: int


class DayPoint(_Model):
    day: date
    calls: int
    credits: int
    errors: int


class HeliusResponse(_Model):
    rpc_configured: bool
    provider: str | None
    plan: HeliusPlan
    credits: HeliusCredits
    calls: HeliusCalls
    methods: list[HeliusMethod]
    hourly: list[HourPoint]
    daily: list[DayPoint]
    credit_costs: dict[str, int]
    default_credit_cost: int


class Breaker(_Model):
    state: str | None
    failures: int
    open_until: datetime | None


class UpstreamRow(_Model):
    source: str
    calls: int
    errors: int
    rate_limited: int
    error_rate: float
    avg_ms: float | None
    max_ms: int
    peak_rps: int
    last_seen: datetime | None
    methods: dict[str, int]
    breaker: Breaker | None = None


class UpstreamHour(_Model):
    hour: datetime
    source: str
    calls: int
    errors: int


class BreakerRow(Breaker):
    source: str
    updated_at: datetime | None


class UpstreamsResponse(_Model):
    hours: int
    sources: list[UpstreamRow]
    hourly: list[UpstreamHour]
    breakers: list[BreakerRow]


class DepthTiming(_Model):
    depth: str | None
    done: int
    failed: int
    wait_p50_s: float | None
    wait_p95_s: float | None
    run_p50_s: float | None
    run_p95_s: float | None
    total_p50_s: float | None
    total_p95_s: float | None
    total_max_s: float | None


class KindCounts(_Model):
    kind: str
    pending: int
    running: int
    done: int
    failed: int


class ErrorCount(_Model):
    error_code: str | None
    count: int
    last_error: str | None
    last_at: datetime | None


class QueueHour(_Model):
    hour: datetime
    created: int
    done: int
    failed: int


class Requester(_Model):
    requested_by: str | None
    jobs: int


class JobsResponse(_Model):
    hours: int
    by_depth: list[DepthTiming]
    by_kind: list[KindCounts]
    errors: list[ErrorCount]
    hourly: list[QueueHour]
    requesters: list[Requester]
    retried: int
    stuck_running: int


class TrendSourceRow(_Model):
    source: str
    status: str
    reads: int
    newest_as_of: datetime | None


class KnowledgeTable(_Model):
    name: str
    rows: int
    newest: datetime | date | None


class PaidX(_Model):
    enabled: bool
    calls_today: int
    spend_today_usd: float
    cap_usd: float


class AnalysisCount(_Model):
    depth: str
    analyses: int
    tokens: int
    with_referent: int


class SignalsResponse(_Model):
    hours: int
    trend_sources: list[TrendSourceRow]
    knowledge: list[KnowledgeTable]
    paid_x: PaidX
    analyses: list[AnalysisCount]


# ---------------------------------------------------------------- helius


@router.get("/helius", response_model=HeliusResponse, responses=ADMIN_RESPONSES)
async def helius(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    hours: Annotated[int, Query(ge=1, le=24 * 14)] = 48,
) -> HeliusResponse:
    """Solana RPC (Helius) calls and credits: this hour, today, the last 24 h, the billing
    cycle so far and its projection against the plan; per method; hourly and daily series.
    Credits are counted from each method's Helius price (HELIUS_CREDIT_COSTS overrides)."""
    s = request.app.state.settings
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = billing_cycle(now, s.helius_billing_day)
    d24, d7 = now - timedelta(hours=24), now - timedelta(days=7)
    async with _pool(request).acquire() as conn:
        # $1 this hour, $2 today, $3 cycle start, $4 24 h ago, $5 7 days ago, $6 source
        tot = await conn.fetchrow(
            """select
                 coalesce(sum(credits) filter (where hour = $1), 0) as this_hour,
                 coalesce(sum(credits) filter (where hour = $1 - interval '1 hour'), 0)
                   as last_hour,
                 coalesce(sum(credits) filter (where hour >= $2), 0) as today,
                 coalesce(sum(credits) filter (where hour > $4), 0) as last_24h,
                 coalesce(sum(credits) filter (where hour > $5), 0) as last_7d,
                 coalesce(sum(credits) filter (where hour >= $3), 0) as cycle,
                 coalesce(sum(calls) filter (where hour >= $2), 0) as calls_today,
                 coalesce(sum(errors) filter (where hour >= $2), 0) as errors_today,
                 coalesce(sum(rate_limited) filter (where hour >= $2), 0) as rl_today,
                 coalesce(sum(calls) filter (where hour > $4), 0) as calls_24h,
                 coalesce(sum(errors) filter (where hour > $4), 0) as errors_24h,
                 coalesce(sum(rate_limited) filter (where hour > $4), 0) as rl_24h,
                 sum(ms_total) filter (where hour > $4) as ms_24h,
                 coalesce(max(peak_rps) filter (where hour > $4), 0) as peak_24h
               from upstream_usage
               where source = $6 and hour >= least($3, $5)""",
            hour,
            today,
            start,
            d24,
            d7,
            RPC,
        )
        methods = await conn.fetch(
            """select method,
                 coalesce(sum(calls) filter (where hour >= $1), 0) as calls_today,
                 coalesce(sum(credits) filter (where hour >= $1), 0) as credits_today,
                 coalesce(sum(errors) filter (where hour >= $1), 0) as errors_today,
                 coalesce(sum(calls) filter (where hour > $3), 0) as calls_24h,
                 coalesce(sum(credits) filter (where hour > $3), 0) as credits_24h,
                 sum(ms_total) filter (where hour > $3) as ms_24h,
                 coalesce(sum(calls) filter (where hour >= $2), 0) as calls_cycle,
                 coalesce(sum(credits) filter (where hour >= $2), 0) as credits_cycle
               from upstream_usage
               where source = $4 and hour >= least($2, $3)
               group by method order by credits_cycle desc, method""",
            today,
            start,
            d24,
            RPC,
        )
        hourly = await conn.fetch(
            """select g.hour, coalesce(sum(u.calls), 0) as calls,
                      coalesce(sum(u.credits), 0) as credits,
                      coalesce(sum(u.errors), 0) as errors,
                      coalesce(sum(u.rate_limited), 0) as rate_limited,
                      coalesce(max(u.peak_rps), 0) as peak_rps
               from generate_series($1, $2, interval '1 hour') as g(hour)
               left join upstream_usage u on u.hour = g.hour and u.source = $3
               group by g.hour order by g.hour""",
            hour - timedelta(hours=hours - 1),
            hour,
            RPC,
        )
        daily = await conn.fetch(
            """select (g.day at time zone 'utc')::date as day, coalesce(sum(u.calls), 0) as calls,
                      coalesce(sum(u.credits), 0) as credits,
                      coalesce(sum(u.errors), 0) as errors
               from generate_series($1, $2, interval '1 day') as g(day)
               left join upstream_usage u on u.source = $3
                 and u.hour >= g.day and u.hour < g.day + interval '1 day'
               group by g.day order by g.day""",
            today - timedelta(days=days - 1),
            today,
            RPC,
        )
        # the API service may not hold SOLANA_RPC_URL (only the analyzer needs it): any
        # RPC call recorded in the last week also shows the analyzer has one
        seen = await conn.fetchval(
            "select exists(select 1 from upstream_usage where source = $1 and hour > $2)",
            RPC,
            d7,
        )
    plan = max(1, s.helius_plan_credits)
    cycle = int(tot["cycle"])
    per_day = float(tot["last_24h"])
    days_left = max(0.0, (end - now).total_seconds() / 86400)
    projected = int(cycle + per_day * days_left)
    left = plan - cycle
    response.headers["Cache-Control"] = "no-store"
    calls_24h = int(tot["calls_24h"])
    return HeliusResponse(
        rpc_configured=bool(s.solana_rpc_url) or bool(seen),
        provider=metrics.provider_of(s.solana_rpc_url) if s.solana_rpc_url else None,
        plan=HeliusPlan(
            credits_per_cycle=s.helius_plan_credits,
            rps_limit=s.helius_rps_limit,
            billing_day=s.helius_billing_day,
            cycle_start=start,
            cycle_end=end,
        ),
        credits=HeliusCredits(
            this_hour=int(tot["this_hour"]),
            last_hour=int(tot["last_hour"]),
            today=int(tot["today"]),
            last_24h=int(tot["last_24h"]),
            last_7d=int(tot["last_7d"]),
            cycle_to_date=cycle,
            cycle_pct=round(100 * cycle / plan, 2),
            per_day_now=per_day,
            projected_cycle=projected,
            projected_pct=round(100 * projected / plan, 2),
            days_left_in_cycle=round(days_left, 2),
            days_until_exhausted=(
                0.0 if left <= 0 else round(left / per_day, 2) if per_day > 0 else None
            ),
        ),
        calls=HeliusCalls(
            today=int(tot["calls_today"]),
            errors_today=int(tot["errors_today"]),
            rate_limited_today=int(tot["rl_today"]),
            last_24h=calls_24h,
            errors_24h=int(tot["errors_24h"]),
            rate_limited_24h=int(tot["rl_24h"]),
            avg_ms_24h=_f(tot["ms_24h"] / calls_24h) if calls_24h and tot["ms_24h"] else None,
            peak_rps_24h=int(tot["peak_24h"]),
        ),
        methods=[
            HeliusMethod(
                method=m["method"],
                credit_cost=metrics.credits_for(m["method"]),
                calls_today=int(m["calls_today"]),
                credits_today=int(m["credits_today"]),
                errors_today=int(m["errors_today"]),
                calls_24h=int(m["calls_24h"]),
                credits_24h=int(m["credits_24h"]),
                avg_ms_24h=(
                    _f(m["ms_24h"] / m["calls_24h"]) if m["calls_24h"] and m["ms_24h"] else None
                ),
                calls_cycle=int(m["calls_cycle"]),
                credits_cycle=int(m["credits_cycle"]),
            )
            for m in methods
        ],
        hourly=[HourPoint(**dict(r)) for r in hourly],
        daily=[DayPoint(**dict(r)) for r in daily],
        credit_costs=dict(metrics.meter.credit_costs),
        default_credit_cost=metrics.DEFAULT_CREDITS,
    )


# ---------------------------------------------------------------- upstreams


@router.get("/upstreams", response_model=UpstreamsResponse, responses=ADMIN_RESPONSES)
async def upstreams(
    request: Request,
    response: Response,
    hours: Annotated[int, Query(ge=1, le=24 * 14)] = 24,
) -> UpstreamsResponse:
    """Every upstream's calls, errors, 429s and latency over `hours`, its circuit breaker,
    and an hourly series. Hosts nothing names (metadata URIs) count as `other`."""
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """select source, sum(calls) as calls, sum(errors) as errors,
                      sum(rate_limited) as rate_limited, sum(ms_total) as ms_total,
                      max(ms_max) as ms_max, max(peak_rps) as peak_rps, max(hour) as last_seen,
                      jsonb_object_agg(method, calls) as methods
               from (select source, method, sum(calls) as calls, sum(errors) as errors,
                            sum(rate_limited) as rate_limited, sum(ms_total) as ms_total,
                            max(ms_max) as ms_max, max(peak_rps) as peak_rps, max(hour) as hour
                     from upstream_usage where hour > now() - make_interval(hours => $1)
                     group by source, method) m
               group by source order by sum(calls) desc""",
            hours,
        )
        hourly = await conn.fetch(
            """select hour, source, sum(calls) as calls, sum(errors) as errors
               from upstream_usage where hour > now() - make_interval(hours => $1)
               group by hour, source order by hour, source""",
            hours,
        )
        breakers = await conn.fetch(
            """select source, state, failures, open_until, updated_at from source_health
               order by (state = 'open') desc, failures desc, source"""
        )
    response.headers["Cache-Control"] = "no-store"
    by_source = {b["source"]: b for b in breakers}
    out = []
    for r in rows:
        calls = int(r["calls"])
        b = by_source.get(r["source"])
        out.append(
            UpstreamRow(
                source=r["source"],
                calls=calls,
                errors=int(r["errors"]),
                rate_limited=int(r["rate_limited"]),
                error_rate=round(int(r["errors"]) / calls, 4) if calls else 0.0,
                avg_ms=_f(r["ms_total"] / calls) if calls else None,
                max_ms=int(r["ms_max"]),
                peak_rps=int(r["peak_rps"]),
                last_seen=r["last_seen"],
                methods={k: int(v) for k, v in _jsonb(r["methods"]).items()},
                breaker=(
                    Breaker(state=b["state"], failures=b["failures"], open_until=b["open_until"])
                    if b
                    else None
                ),
            )
        )
    return UpstreamsResponse(
        hours=hours,
        sources=out,
        hourly=[UpstreamHour(**dict(h)) for h in hourly],
        breakers=[BreakerRow(**dict(b)) for b in breakers],
    )


# ---------------------------------------------------------------- jobs


# Jobs created, done and failed per hour over the last $1 hours, every hour listed: one pass
# over the window for each of created_at and finished_at, not one scan of the job table per
# hour (no join, so the row estimate stays small and the planner does not JIT-compile it).
JOBS_HOURLY_SQL = """
    select hour, sum(created)::bigint as created, sum(done)::bigint as done,
           sum(failed)::bigint as failed
    from (
      select generate_series(date_trunc('hour', now()) - make_interval(hours => $1 - 1),
                             date_trunc('hour', now()), interval '1 hour') as hour,
             0 as created, 0 as done, 0 as failed
      union all
      select date_trunc('hour', created_at), 1, 0, 0 from job
      where created_at >= date_trunc('hour', now()) - make_interval(hours => $1 - 1)
      union all
      select date_trunc('hour', finished_at), 0, (status = 'done')::int,
             (status = 'failed')::int
      from job
      where status in ('done', 'failed')
        and finished_at >= date_trunc('hour', now()) - make_interval(hours => $1 - 1)
    ) as t
    group by hour order by hour"""


@router.get("/queue", response_model=JobsResponse, responses=ADMIN_RESPONSES)
async def queue_stats(
    request: Request,
    response: Response,
    hours: Annotated[int, Query(ge=1, le=24 * 7)] = 24,
) -> JobsResponse:
    """Job timing by depth (queue wait, processing and total, p50/p95), counts by kind,
    failures by error code, hourly throughput and who asked, over `hours`. Jobs are kept
    for 7 days."""
    async with _pool(request).acquire() as conn:
        depth = await conn.fetch(
            """select depth,
                 count(*) filter (where status = 'done') as done,
                 count(*) filter (where status = 'failed') as failed,
                 percentile_cont(0.5) within group (order by extract(epoch from
                   started_at - created_at)) filter (where status = 'done') as wait_p50,
                 percentile_cont(0.95) within group (order by extract(epoch from
                   started_at - created_at)) filter (where status = 'done') as wait_p95,
                 percentile_cont(0.5) within group (order by extract(epoch from
                   finished_at - started_at)) filter (where status = 'done') as run_p50,
                 percentile_cont(0.95) within group (order by extract(epoch from
                   finished_at - started_at)) filter (where status = 'done') as run_p95,
                 percentile_cont(0.5) within group (order by extract(epoch from
                   finished_at - created_at)) filter (where status = 'done') as total_p50,
                 percentile_cont(0.95) within group (order by extract(epoch from
                   finished_at - created_at)) filter (where status = 'done') as total_p95,
                 max(extract(epoch from finished_at - created_at))
                   filter (where status = 'done') as total_max
               from job
               where kind = 'analyze' and finished_at > now() - make_interval(hours => $1)
               group by depth order by depth""",
            hours,
        )
        kinds = await conn.fetch(
            """select kind,
                 count(*) filter (where status = 'pending') as pending,
                 count(*) filter (where status = 'running') as running,
                 count(*) filter (where status = 'done'
                   and finished_at > now() - make_interval(hours => $1)) as done,
                 count(*) filter (where status = 'failed'
                   and finished_at > now() - make_interval(hours => $1)) as failed
               from job
               where status in ('pending', 'running')
                  or finished_at > now() - make_interval(hours => $1)
               group by kind order by kind""",
            hours,
        )
        errs = await conn.fetch(
            """select error_code, count(*) as count,
                      (array_agg(last_error order by finished_at desc))[1] as last_error,
                      max(finished_at) as last_at
               from job
               where status = 'failed' and finished_at > now() - make_interval(hours => $1)
               group by error_code order by count desc limit 20""",
            hours,
        )
        hourly = await conn.fetch(JOBS_HOURLY_SQL, hours)
        who = await conn.fetch(
            """select requested_by, count(*) as jobs from job
               where created_at > now() - make_interval(hours => $1)
               group by requested_by order by jobs desc limit 20""",
            hours,
        )
        extra = await conn.fetchrow(
            """select
                 count(*) filter (where attempts > 1
                   and (finished_at > now() - make_interval(hours => $1)
                        or status in ('pending', 'running'))) as retried,
                 count(*) filter (where status = 'running' and locked_until < now())
                   as stuck_running
               from job
               where status in ('pending', 'running')
                  or finished_at > now() - make_interval(hours => $1)""",
            hours,
        )
    response.headers["Cache-Control"] = "no-store"
    return JobsResponse(
        hours=hours,
        by_depth=[
            DepthTiming(
                depth=d["depth"],
                done=d["done"],
                failed=d["failed"],
                wait_p50_s=_f(d["wait_p50"]),
                wait_p95_s=_f(d["wait_p95"]),
                run_p50_s=_f(d["run_p50"]),
                run_p95_s=_f(d["run_p95"]),
                total_p50_s=_f(d["total_p50"]),
                total_p95_s=_f(d["total_p95"]),
                total_max_s=_f(d["total_max"]),
            )
            for d in depth
        ],
        by_kind=[KindCounts(**dict(k)) for k in kinds],
        errors=[ErrorCount(**dict(e)) for e in errs],
        hourly=[QueueHour(**dict(h)) for h in hourly],
        requesters=[Requester(**dict(w)) for w in who],
        retried=int(extra["retried"]),
        stuck_running=int(extra["stuck_running"]),
    )


# ---------------------------------------------------------------- signals


@router.get("/signals", response_model=SignalsResponse, responses=ADMIN_RESPONSES)
async def signals(
    request: Request,
    response: Response,
    hours: Annotated[int, Query(ge=1, le=24 * 7)] = 6,
) -> SignalsResponse:
    """What the engine's inputs look like: each trend source's status as recent full reads
    saw it, how fresh the knowledge tables are, paid X spend against its cap, and analyses
    per depth with how many found a referent."""
    s = request.app.state.settings
    async with _pool(request).acquire() as conn:
        trend = await conn.fetch(
            """select src->>'source' as source, src->>'status' as status, count(*) as reads,
                      max((src->>'as_of')::timestamptz) as newest_as_of
               from analysis a,
                    jsonb_array_elements(coalesce(a.doc->'trend'->'sources', '[]'::jsonb)) src
               where a.created_at > now() - make_interval(hours => $1)
               group by 1, 2 order by 1, 2""",
            hours,
        )
        know = await conn.fetchrow(
            """select
                 (select count(*) from trend_term where day >= current_date - 1) as trend_rows,
                 (select max(day) from trend_term) as trend_newest,
                 (select count(*) from known_coin) as known_rows,
                 (select max(updated_at) from known_coin) as known_newest,
                 (select count(*) from top_volume
                    where day = (select max(day) from top_volume)) as tv_rows,
                 (select max(day) from top_volume) as tv_newest,
                 (select count(*) from entity) as entity_rows,
                 (select count(*) from token) as token_rows,
                 (select max(created_at) from token) as token_newest"""
        )
        paid = await conn.fetchval(
            """select requests from api_usage
               where key_name = $1 and day = (now() at time zone 'utc')::date""",
            PAID_X_USAGE_KEY,
        )
        analyses = await conn.fetch(
            """select depth, count(*) as analyses, count(distinct mint) as tokens,
                      count(*) filter (where referent is not null) as with_referent
               from analysis where created_at > now() - make_interval(hours => $1)
               group by depth order by depth""",
            hours,
        )
    response.headers["Cache-Control"] = "no-store"
    calls = int(paid or 0)
    return SignalsResponse(
        hours=hours,
        trend_sources=[TrendSourceRow(**dict(t)) for t in trend],
        knowledge=[
            KnowledgeTable(name="trend_term", rows=know["trend_rows"], newest=know["trend_newest"]),
            KnowledgeTable(name="known_coin", rows=know["known_rows"], newest=know["known_newest"]),
            KnowledgeTable(name="top_volume", rows=know["tv_rows"], newest=know["tv_newest"]),
            KnowledgeTable(name="entity", rows=know["entity_rows"], newest=None),
            KnowledgeTable(name="token", rows=know["token_rows"], newest=know["token_newest"]),
        ],
        paid_x=PaidX(
            enabled=bool(s.enable_paid_x and s.twitterapi_io_key),
            calls_today=calls,
            spend_today_usd=round(calls * s.paid_x_usd_per_call, 4),
            cap_usd=s.paid_x_daily_usd_cap,
        ),
        analyses=[AnalysisCount(**dict(a)) for a in analyses],
    )
