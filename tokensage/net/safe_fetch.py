"""Fetch attacker-controlled URLs safely (guide §10).

- https only
- DNS is resolved up front and every address must be public (no private, loopback,
  link-local, multicast, reserved, or cloud-metadata ranges)
- redirects are followed manually (<= 3 hops) and each hop is re-checked
- byte and time caps, streamed; no cookies, no credentials

Known limitation: the check-then-connect sequence leaves a small DNS-rebinding window.
Acceptable for v1 given there are no internal services to reach from these containers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[..., Awaitable[list[Any]]]
# Tests inject a fake resolver here; production leaves it None (= loop.getaddrinfo).
DEFAULT_RESOLVER: Resolver | None = None

MAX_REDIRECTS = 3


def _dev_insecure() -> bool:
    from tokensage.config import get_settings

    return bool(get_settings().dev_allow_insecure_fetch)


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


class UnsafeUrl(ValueError):
    pass


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Fetched:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes
    truncated: bool = False


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
        # anything else not globally routable, e.g. 100.64.0.0/10 carrier-grade NAT, which
        # holds cloud metadata endpoints such as 100.100.100.200
        or not ip.is_global
    )


async def check_url(url: str, resolver: Resolver | None = None) -> str:
    """Validate scheme/host and resolve DNS; return the normalised URL or raise UnsafeUrl.
    Malformed URLs (bad port, invalid IDNA label, ...) are UnsafeUrl too, never a bare
    ValueError/UnicodeError escaping to the caller."""
    try:
        return await _check_url(url, resolver)
    except (UnsafeUrl, FetchError):
        raise
    except (ValueError, UnicodeError) as e:
        raise UnsafeUrl(f"malformed url: {type(e).__name__}: {e}"[:200]) from e


async def _check_url(url: str, resolver: Resolver | None) -> str:
    parts = urlsplit(url)
    if _dev_insecure():
        return url  # DEV ONLY (see Settings.dev_allow_insecure_fetch)
    if parts.scheme != "https":
        raise UnsafeUrl(f"scheme not allowed: {parts.scheme or 'none'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeUrl(f"host not allowed: {host or 'empty'}")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in URL")
    port = parts.port  # raises ValueError for out-of-range / non-numeric ports
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise UnsafeUrl(f"ip not public: {literal}")
        return url
    loop = asyncio.get_running_loop()
    try:
        fn: Resolver = resolver or DEFAULT_RESOLVER or loop.getaddrinfo
        infos = await fn(host, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as e:
        raise FetchError(f"dns failure for {host}: {e}") from e
    if not infos:
        raise FetchError(f"dns returned nothing for {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            raise UnsafeUrl(f"{host} resolves to non-public {ip}")
    return url


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    accept: str = "*/*",
    resolver: Resolver | None = None,
) -> Fetched:
    """GET with the guard applied to the URL and every redirect hop. `timeout` bounds the
    whole fetch (all hops, headers and body), not just each socket read."""
    try:
        async with asyncio.timeout(timeout):
            return await _safe_get(client, url, max_bytes, timeout, accept, resolver)
    except TimeoutError as e:
        raise FetchError(f"timeout: no complete response within {timeout:.0f}s") from e


async def _safe_get(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    timeout: float,
    accept: str,
    resolver: Resolver | None,
) -> Fetched:
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        await check_url(current, resolver=resolver)
        try:
            async with client.stream(
                "GET",
                current,
                # identity: the byte cap applies to what we hold in memory; a compressed
                # body could otherwise inflate far past it within one chunk
                headers={"Accept": accept, "Accept-Encoding": "identity"},
                timeout=timeout,
                follow_redirects=False,
            ) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location")
                    if not loc:
                        raise FetchError("redirect without location", r.status_code, False)
                    current = urljoin(current, loc)
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    raise FetchError(f"http {r.status_code}", r.status_code, retryable=True)
                if r.status_code != 200:
                    raise FetchError(f"http {r.status_code}", r.status_code, retryable=False)
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"too large: {declared} bytes", 200, retryable=False)
                buf = bytearray()
                truncated = False
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        break
                if truncated:
                    raise FetchError(f"too large: > {max_bytes} bytes", 200, retryable=False)
                return Fetched(
                    url=current,
                    status=r.status_code,
                    content_type=r.headers.get("content-type", "").split(";")[0].strip().lower(),
                    body=bytes(buf),
                )
        except httpx.TimeoutException as e:
            raise FetchError(f"timeout: {e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__}: {e}", retryable=True) from e
    raise FetchError("too many redirects", retryable=False)
