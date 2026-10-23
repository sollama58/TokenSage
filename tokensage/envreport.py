"""Which of the env vars each TokenSage service is meant to have are set on it, for
TrenchScanner's Admin > Environment (user ask 2026-10-23). Render keeps each service's
variables apart, and one added to the api is easily missed on the analyzer (TRUSTED_HINT_KEYS).

The api reports live; the analyzer and the two crons write their report into service_env when
they start (a worker has no port the api could ask), and GET /admin/v1/env lays them side by
side. A report never carries a value that could be a credential: a secret is its length and a
fingerprint (compared across services, never sent), a URL its host, and only plain settings
their value."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

import asyncpg

Need = Literal["required", "expected", "optional"]
Kind = Literal["secret", "url", "setting"]
State = Literal["set", "empty", "missing"]

# The services that report, in the order the grid shows them. TOKENSAGE_ROLE "worker" is the
# analyzer.
SERVICES: tuple[tuple[str, str], ...] = (
    ("api", "tokensage-api"),
    ("analyzer", "tokensage-analyzer"),
    ("knowledge", "knowledge cron"),
    ("maintenance", "maintenance cron"),
)
ROLE_SERVICE = {
    "api": "api",
    "worker": "analyzer",
    "knowledge": "knowledge",
    "maintenance": "maintenance",
}


@dataclass(frozen=True)
class EnvVar:
    key: str
    group: str
    kind: Kind
    on: Mapping[str, Need]
    same: bool = False  # meant to hold the same value everywhere it is (compared, never shown)
    note: str | None = None


_ALL: dict[str, Need] = {s: "expected" for s, _ in SERVICES}
_SHARED_GROUP = (
    "LOG_LEVEL",
    "HTTP_USER_AGENT",
    "ACCEPT_NON_PUMP",
    "ENABLE_CLIP",
    "VISION_MODEL_PATH",
    "ENABLE_EMBED",
    "ENABLE_CORPUS",
    "ENABLE_PAID_X",
    "PAID_X_DAILY_USD_CAP",
    "IPFS_GATEWAYS",
    "HELIUS_PLAN_CREDITS",
    "HELIUS_RPS_LIMIT",
    "HELIUS_BILLING_DAY",
)

# Every variable render.yaml gives each service (tests/test_envreport.py holds this to it).
MANIFEST: tuple[EnvVar, ...] = (
    EnvVar("TOKENSAGE_ROLE", "Core", "setting", {s: "required" for s, _ in SERVICES}),
    EnvVar("DATABASE_URL", "Core", "url", {s: "required" for s, _ in SERVICES}, same=True),
    EnvVar("WEB_CONCURRENCY", "Core", "setting", {"api": "expected"}),
    EnvVar("INLINE_ANALYZER", "Core", "setting", {"api": "expected"}),
    EnvVar("WORKER_CONCURRENCY", "Core", "setting", {"analyzer": "expected"}),
    EnvVar("OCR_CONCURRENCY", "Core", "setting", {"analyzer": "expected"}),
    EnvVar(
        "API_KEYS",
        "Keys",
        "secret",
        {"api": "optional"},
        note="Consumer keys can live in the admin key table instead.",
    ),
    EnvVar("ADMIN_KEY", "Keys", "secret", {"api": "required"}),
    EnvVar(
        "MONITOR_KEY",
        "Keys",
        "secret",
        {"api": "expected"},
        note="TrenchScanner's read-only key for these monitoring reads.",
    ),
    EnvVar(
        "TRUSTED_HINT_KEYS",
        "Keys",
        "setting",
        {"analyzer": "expected"},
        note='Key names whose hints are first-party ("trenchscanner"): keeps X-reuse whole.',
    ),
    EnvVar("FULL_PER_DAY_DEFAULT", "Keys", "setting", {"api": "expected"}),
    EnvVar("RATE_PER_MIN_DEFAULT", "Keys", "setting", {"api": "expected"}),
    EnvVar("SOLANA_RPC_URL", "Upstreams", "url", {"analyzer": "required"}),
    EnvVar(
        "SOLANA_WS_URL",
        "Upstreams",
        "url",
        {"analyzer": "optional"},
        note="Only with ENABLE_CORPUS.",
    ),
    EnvVar(
        "COINGECKO_API_KEY",
        "Upstreams",
        "secret",
        {"analyzer": "expected", "knowledge": "expected"},
        same=True,
    ),
    EnvVar(
        "TWITTERAPI_IO_KEY",
        "Upstreams",
        "secret",
        {"analyzer": "optional"},
        note="Paid X fallback.",
    ),
    *(EnvVar(k, "Shared settings", "setting", _ALL, same=True) for k in _SHARED_GROUP),
)


@dataclass
class VarReport:
    state: State
    hint: str | None = None
    print: str | None = field(default=None)  # a secret's or URL's short SHA-256; never sent on

    def as_json(self) -> dict[str, str | None]:
        return {"state": self.state, "hint": self.hint, "print": self.print}


MAX_HINT = 80


def describe(kind: Kind, raw: str | None) -> VarReport:
    if raw is None:
        return VarReport("missing")
    value = raw.strip()
    if not value:
        return VarReport("empty")
    if kind == "setting":
        return VarReport("set", value if len(value) <= MAX_HINT else value[: MAX_HINT - 1] + "…")
    fp = hashlib.sha256(value.encode()).hexdigest()[:12]
    if kind == "url":
        try:
            u = urlsplit(value)
            host, port = u.hostname, u.port
        except ValueError:
            return VarReport("set", "set, not a URL", fp)
        if not u.scheme or not host:
            return VarReport("set", "set, not a URL", fp)
        web = u.scheme in ("http", "https")
        hint = (f"{host}:{port}" if port else host) if web else f"{u.scheme}://{host}"
        return VarReport("set", hint, fp)
    return VarReport("set", f"{len(value)} chars", fp)


def report(env: Mapping[str, str] | None = None) -> dict[str, VarReport]:
    env = os.environ if env is None else env
    return {v.key: describe(v.kind, env.get(v.key)) for v in MANIFEST}


def commit(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return (env.get("RENDER_GIT_COMMIT") or "")[:12] or None


async def publish(
    conn: asyncpg.Connection | asyncpg.Pool, service: str, env: Mapping[str, str] | None = None
) -> None:
    """Writes this process's report. Render applies an env change only by restarting the
    service, so the report a process writes when it starts holds until its next start."""
    vars_ = {k: v.as_json() for k, v in report(env).items()}
    await conn.execute(
        """insert into service_env (service, vars, commit, reported_at)
           values ($1, $2::text::jsonb, $3, now())
           on conflict (service) do update
             set vars = excluded.vars, commit = excluded.commit,
                 reported_at = excluded.reported_at""",
        service,
        json.dumps(vars_),
        commit(env),
    )


def read_vars(raw: object) -> dict[str, VarReport]:
    """A stored report read back: only well-formed entries."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    out: dict[str, VarReport] = {}
    if not isinstance(raw, dict):
        return out
    for key, v in raw.items():
        if not isinstance(v, dict) or v.get("state") not in ("set", "empty", "missing"):
            continue
        hint, fp = v.get("hint"), v.get("print")
        out[str(key)] = VarReport(
            v["state"], hint if isinstance(hint, str) else None, fp if isinstance(fp, str) else None
        )
    return out


def differs(reports: list[VarReport | None]) -> bool:
    seen = {r.print or r.hint or "" for r in reports if r is not None and r.state == "set"}
    return len(seen) > 1


async def publish_quietly(target: asyncpg.Pool | str, role: str) -> None:
    """publish() for a process starting up, through its pool or a DSN of its own: a report
    that can't be written is logged, never in the way of the process's own work."""
    import structlog

    try:
        if isinstance(target, str):
            conn = await asyncpg.connect(target, timeout=10)
            try:
                await publish(conn, ROLE_SERVICE.get(role, role))
            finally:
                await conn.close()
        else:
            await publish(target, ROLE_SERVICE.get(role, role))
    except Exception as e:  # noqa: BLE001
        structlog.get_logger("envreport").warning("envreport.not_written", error=str(e)[:200])
