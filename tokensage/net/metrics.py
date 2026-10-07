"""Upstream call metering for the admin panel: every outbound call is counted per hour,
source and method (calls, errors, 429s, latency, Helius credits), in process, and flushed to
upstream_usage every FLUSH_INTERVAL_S. Several processes add into the same rows.

Two places record: MeteredTransport, under every shared httpx client, labels plain HTTP calls
by upstream (wikipedia, google_news, ipfs, ...), and SolanaRpc records each JSON-RPC call
by method with its Helius credit cost. The transport skips the RPC host so a call is never
counted twice."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import asyncpg
import httpx
import structlog

log = structlog.get_logger("metrics")

FLUSH_INTERVAL_S = 30.0

# Helius credits per call (https://www.helius.dev/docs/billing/credits). Standard RPC
# methods cost 1; anything not listed here costs DEFAULT_CREDITS. HELIUS_CREDIT_COSTS
# ("method:credits,...") overrides entries when Helius changes its prices.
HELIUS_CREDITS: dict[str, int] = {
    "getProgramAccounts": 10,
    "getAsset": 10,
    "getAssetBatch": 10,
    "getAssetsByOwner": 10,
    "getAssetsByGroup": 10,
    "getAssetsByCreator": 10,
    "getAssetsByAuthority": 10,
    "searchAssets": 10,
    "getAssetProof": 10,
    "getSignaturesForAsset": 10,
    "getTokenAccounts": 10,
    "getValidityProof": 100,
}
DEFAULT_CREDITS = 1

# host suffix -> source label, for the panel's per-upstream table
HOSTS: list[tuple[str, str]] = [
    ("helius-rpc.com", "solana_rpc"),
    ("helius.xyz", "solana_rpc"),
    ("api.twitterapi.io", "x_paid"),
    ("cdn.syndication.twimg.com", "x_syndication"),
    ("publish.x.com", "x_oembed"),
    ("api.fxtwitter.com", "x_fxtwitter"),
    ("api.vxtwitter.com", "x_vxtwitter"),
    ("pbs.twimg.com", "x_media"),
    ("wikipedia.org", "wikipedia"),
    ("wikimedia.org", "wikimedia"),
    ("wikidata.org", "wikidata"),
    ("news.google.com", "google_news"),
    ("trends.google.com", "google_trends"),
    ("api.geckoterminal.com", "geckoterminal"),
    ("api.coingecko.com", "coingecko"),
    ("pro-api.coingecko.com", "coingecko"),
    ("api.dexscreener.com", "dexscreener"),
    ("pump.fun", "pump_fun"),
]
OTHER = "other"


def parse_costs(spec: str) -> dict[str, int]:
    """'getAsset:10,getTransaction:1' -> {'getAsset': 10, 'getTransaction': 1}."""
    out: dict[str, int] = {}
    for item in spec.split(","):
        name, _, n = item.strip().partition(":")
        try:
            if name.strip() and int(n) >= 0:
                out[name.strip()] = int(n)
        except ValueError:
            continue
    return out


def credits_for(method: str) -> int:
    return meter.credit_costs.get(method, DEFAULT_CREDITS)


def classify(host: str) -> str:
    host = host.lower().rstrip(".")
    if host in meter.ipfs_hosts:
        return "ipfs"
    for suffix, label in HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return label
    return OTHER


def provider_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return "helius" if "helius" in host else (host or "unknown")


@dataclass
class Counter:
    calls: int = 0
    errors: int = 0
    rate_limited: int = 0
    credits: int = 0
    ms_total: int = 0
    ms_max: int = 0
    peak_rps: int = 0


def _hour(ts: float) -> int:
    return int(ts // 3600 * 3600)


class Meter:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str, str], Counter] = {}
        self.credit_costs: dict[str, int] = dict(HELIUS_CREDITS)
        self.ipfs_hosts: set[str] = set()
        self.skip_hosts: set[str] = set()
        self._sec: dict[str, tuple[int, int]] = {}  # source -> (epoch second, calls in it)

    def configure(self, *, rpc_url: str = "", ipfs_gateways: list[str], costs: str = "") -> None:
        self.ipfs_hosts = {(urlsplit(g).hostname or "").lower() for g in ipfs_gateways} - {""}
        host = (urlsplit(rpc_url).hostname or "").lower()
        self.skip_hosts = {host} if host else set()
        self.credit_costs = {**HELIUS_CREDITS, **parse_costs(costs)}

    def record(
        self,
        source: str,
        method: str,
        *,
        ok: bool,
        ms: float,
        rate_limited: bool = False,
        credits: int = 0,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        c = self.rows.setdefault((_hour(now), source, method), Counter())
        c.calls += 1
        c.errors += 0 if ok else 1
        c.rate_limited += 1 if rate_limited else 0
        c.credits += credits
        c.ms_total += int(ms)
        c.ms_max = max(c.ms_max, int(ms))
        # requests per second per source (all methods), the number plan rate limits cap;
        # kept on the method row that hit the peak, and maxed per source when read
        sec = int(now)
        last, n = self._sec.get(source, (sec, 0))
        n = n + 1 if last == sec else 1
        self._sec[source] = (sec, n)
        c.peak_rps = max(c.peak_rps, n)

    def take(self) -> dict[tuple[int, str, str], Counter]:
        rows, self.rows = self.rows, {}
        return rows

    def put_back(self, rows: dict[tuple[int, str, str], Counter]) -> None:
        for k, c in rows.items():
            mine = self.rows.setdefault(k, Counter())
            mine.calls += c.calls
            mine.errors += c.errors
            mine.rate_limited += c.rate_limited
            mine.credits += c.credits
            mine.ms_total += c.ms_total
            mine.ms_max = max(mine.ms_max, c.ms_max)
            mine.peak_rps = max(mine.peak_rps, c.peak_rps)


meter = Meter()


async def flush(conn: asyncpg.Connection) -> int:
    """Add the counted calls into upstream_usage and mirror the circuit breakers into
    source_health. Returns the rows written; on a DB error the counts are kept for next time."""
    from tokensage.net.breaker import breaker

    rows = meter.take()
    if rows:
        try:
            await conn.executemany(
                """insert into upstream_usage (hour, source, method, calls, errors,
                     rate_limited, credits, ms_total, ms_max, peak_rps)
                   values (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8, $9, $10)
                   on conflict (hour, source, method) do update set
                     calls = upstream_usage.calls + excluded.calls,
                     errors = upstream_usage.errors + excluded.errors,
                     rate_limited = upstream_usage.rate_limited + excluded.rate_limited,
                     credits = upstream_usage.credits + excluded.credits,
                     ms_total = upstream_usage.ms_total + excluded.ms_total,
                     ms_max = greatest(upstream_usage.ms_max, excluded.ms_max),
                     peak_rps = greatest(upstream_usage.peak_rps, excluded.peak_rps)""",
                [
                    (h, s, m, c.calls, c.errors, c.rate_limited, c.credits, c.ms_total)
                    + (c.ms_max, c.peak_rps)
                    for (h, s, m), c in rows.items()
                ],
            )
        except Exception:
            meter.put_back(rows)
            raise
    await breaker.persist(conn)
    return len(rows)


async def run_flusher(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    """Flush every FLUSH_INTERVAL_S until stop is set, then once more."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=FLUSH_INTERVAL_S)
        except TimeoutError:
            pass
        try:
            async with pool.acquire() as conn:
                await flush(conn)
        except Exception as e:  # noqa: BLE001
            log.warning("metrics.flush_failed", error=f"{type(e).__name__}: {e}")
        if stop.is_set():
            return


class MeteredTransport(httpx.AsyncBaseTransport):
    """Counts each request by upstream. The RPC host is left to SolanaRpc, which knows the
    JSON-RPC method and its credit cost."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None, **kwargs: object):
        self.inner = inner or httpx.AsyncHTTPTransport(**kwargs)  # type: ignore[arg-type]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.lower() in meter.skip_hosts:
            return await self.inner.handle_async_request(request)
        t0 = time.perf_counter()
        source = classify(host)
        try:
            resp = await self.inner.handle_async_request(request)
        except Exception:
            meter.record(source, request.method, ok=False, ms=(time.perf_counter() - t0) * 1000)
            raise
        meter.record(
            source,
            request.method,
            ok=resp.status_code < 400,
            ms=(time.perf_counter() - t0) * 1000,
            rate_limited=resp.status_code == 429,
        )
        return resp

    async def aclose(self) -> None:
        await self.inner.aclose()


def utc_hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
