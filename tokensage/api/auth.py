"""Bearer API keys and per-key rate limiting.

Keys come from two places: the API_KEYS env var ("name:key,name2:key2"), read-only, and the
api_key table, managed through the admin API (/admin/v1/keys). Only SHA-256 digests are kept.
The rate limiter is an in-process token bucket, which is enough while the API runs as one
instance; table keys are re-read periodically so another instance's changes still land.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tokensage.api import errors
from tokensage.config import Settings

_bearer = HTTPBearer(auto_error=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class ApiKey:
    name: str
    rate_per_min: int
    full_per_day: int
    refresh_per_day: int
    digest: str = ""  # sha256 hex of the raw key; also the HMAC secret for callbacks
    source: Literal["env", "db"] = "env"


KEY_PREFIX = "tsk_"


def new_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    buckets: dict[str, _Bucket] = field(default_factory=dict)

    def check(self, key: str, per_min: int) -> int:
        """Consume one token. Returns 0 if allowed, else seconds until the next token."""
        now = time.monotonic()
        b = self.buckets.get(key)
        if b is None:
            b = _Bucket(tokens=float(per_min), updated=now)
            self.buckets[key] = b
        rate = per_min / 60.0
        b.tokens = min(float(per_min), b.tokens + (now - b.updated) * rate)
        b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return 0
        return max(1, int((1 - b.tokens) / rate) + 1)


class KeyStore:
    def __init__(self, settings: Settings):
        self._env: dict[str, ApiKey] = {}
        for name, raw in settings.api_key_pairs.items():
            d = sha256_hex(raw)
            self._env[d] = ApiKey(
                name=name,
                rate_per_min=settings.rate_per_min_default,
                full_per_day=settings.full_per_day_default,
                refresh_per_day=settings.refresh_per_day_default,
                digest=d,
            )
        self._by_digest: dict[str, ApiKey] = dict(self._env)
        self._admin_digest = sha256_hex(settings.admin_key) if settings.admin_key else None
        self.limiter = RateLimiter()

    def lookup(self, raw: str) -> ApiKey | None:
        digest = sha256_hex(raw)
        for d, key in self._by_digest.items():
            if hmac.compare_digest(d, digest):
                return key
        return None

    @property
    def has_keys(self) -> bool:
        return bool(self._by_digest)

    @property
    def env_keys(self) -> list[ApiKey]:
        return list(self._env.values())

    async def reload(self, conn: asyncpg.Connection) -> None:
        """Re-read the active keys from the api_key table. An env key wins a name clash."""
        rows = await conn.fetch(
            """select name, key_sha256, rate_per_min, full_per_day, refresh_per_day
               from api_key where revoked_at is null"""
        )
        env_names = {k.name for k in self._env.values()}
        db = {
            r["key_sha256"]: ApiKey(
                name=r["name"],
                rate_per_min=r["rate_per_min"],
                full_per_day=r["full_per_day"],
                refresh_per_day=r["refresh_per_day"],
                digest=r["key_sha256"],
                source="db",
            )
            for r in rows
            if r["name"] not in env_names
        }
        self._by_digest = {**db, **self._env}

    def is_admin(self, raw: str) -> bool:
        return bool(self._admin_digest) and hmac.compare_digest(
            self._admin_digest or "", sha256_hex(raw)
        )


def get_key_store(request: Request) -> KeyStore:
    return request.app.state.keys


async def require_api_key(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> ApiKey:
    if creds is None or creds.scheme.lower() != "bearer":
        raise errors.unauthorized()
    key = store.lookup(creds.credentials)
    if key is None:
        raise errors.unauthorized()
    wait = store.limiter.check(key.name, key.rate_per_min)
    if wait:
        raise errors.rate_limited(wait)
    request.state.api_key = key
    pool = getattr(request.app.state, "pool", None)
    if pool is not None:
        from tokensage.api import usage

        async with pool.acquire() as conn:
            await usage.bump(conn, key.name, requests=1)
    return key


async def require_admin(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> None:
    if creds is None or not store.is_admin(creds.credentials):
        raise errors.forbidden()
