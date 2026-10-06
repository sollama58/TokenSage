"""Bearer API keys and per-key rate limiting.

v1 reads keys from the API_KEYS env var ("name:key,name2:key2"). Keys are matched by
SHA-256 digest in constant time. The rate limiter is an in-process token bucket, which
is enough while the API runs as one instance.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Annotated

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
        self._by_digest: dict[str, ApiKey] = {}
        for name, raw in settings.api_key_pairs.items():
            self._by_digest[sha256_hex(raw)] = ApiKey(
                name=name,
                rate_per_min=settings.rate_per_min_default,
                full_per_day=settings.full_per_day_default,
                refresh_per_day=settings.refresh_per_day_default,
            )
        self._admin_digest = sha256_hex(settings.admin_key) if settings.admin_key else None
        self.limiter = RateLimiter()

    def lookup(self, raw: str) -> ApiKey | None:
        digest = sha256_hex(raw)
        for d, key in self._by_digest.items():
            if hmac.compare_digest(d, digest):
                return key
        return None

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
    return key


async def require_admin(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> None:
    if creds is None or not store.is_admin(creds.credentials):
        raise errors.forbidden()
