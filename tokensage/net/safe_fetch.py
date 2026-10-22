"""Fetch attacker-controlled URLs safely (guide §10).

- https only
- DNS is resolved up front and every address must be public (no private, loopback,
  link-local, multicast, reserved, or cloud-metadata ranges)
- the TCP connection uses a resolved address: the URL keeps the hostname (so the connection
  pool, TLS SNI, the certificate check and metering all see the real host) and PinnedBackend,
  under every shared client's transport, dials the validated address instead of resolving
  the name again
- redirects are followed manually (<= 3 hops) and each hop is re-checked
- byte and time caps, streamed; no cookies, no credentials
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
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


@dataclass(frozen=True)
class ConnectTarget:
    """How to open one hop: request `url` and dial one of `addresses` for `host`, the name
    httpx connects to. No addresses (dev insecure mode) = resolve as usual."""

    url: str
    host: str
    addresses: tuple[str, ...] = ()


# The addresses prepare_connect validated, by host, for the request this task is sending.
# None = no guarded request in flight: PinnedBackend resolves as usual (RPC, Wikipedia, ...).
_pins: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar("safe_fetch_pins", default=None)


def _pin_key(host: str) -> str:
    return host.lower().rstrip(".")


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Dials the address prepare_connect validated, not whatever the name resolves to now.

    With the IP in the URL instead (SG-001's first version), httpcore keyed its pool by IP and
    reused a connection opened and certificate-checked for one host for any other host on that
    address: two Pinata zones on one Cloudflare IP shared a TLS session, answered 403/421, and
    the second host's certificate was never checked. Every address is tried in order, as
    anyio does for a name, so a dead first record does not fail the fetch."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        pins = _pins.get()
        if pins is None:
            addresses: tuple[str, ...] = (host,)
        else:
            addresses = pins.get(_pin_key(host), ())
            if not addresses:
                # a guarded request must never fall back to a fresh lookup
                raise httpcore.ConnectError(f"no validated address for {host}")
        last: Exception | None = None
        for addr in addresses:
            try:
                return await self.inner.connect_tcp(
                    addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not used")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport on a pool whose connections go through PinnedBackend. A client
    that calls safe_get or delivers callbacks must sit on this (MeteredTransport does): on
    any other transport the validated address is ignored."""

    def __init__(
        self,
        limits: httpx.Limits | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        limits = limits or httpx.Limits(max_connections=100, max_keepalive_connections=20)
        super().__init__(limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PinnedBackend(network_backend),
        )


def public_unsafe_detail(exc: BaseException) -> str:
    """A fixed string for API/analysis storage; never echo resolved internal addresses."""
    if isinstance(exc, UnsafeUrl):
        msg = str(exc)
        if " resolves to non-public " in msg or msg.startswith("ip not public"):
            return "unsafe url"
    return "unsafe url"


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


async def prepare_connect(url: str, resolver: Resolver | None = None) -> ConnectTarget:
    """Validate `url` and return where to connect so DNS cannot change after the check."""
    parts = urlsplit(url)
    if _dev_insecure():
        host = (parts.hostname or "").lower().rstrip(".")
        return ConnectTarget(url, host)
    if parts.scheme != "https":
        raise UnsafeUrl(f"scheme not allowed: {parts.scheme or 'none'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeUrl(f"host not allowed: {host or 'empty'}")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in URL")
    port = parts.port  # raises ValueError for out-of-range / non-numeric ports
    try:
        # the name exactly as httpx will dial it (IDNA 2008, lowercased), so the pin matches
        dial = httpx.URL(url).raw_host.decode("ascii")
    except httpx.InvalidURL as e:
        raise ValueError(str(e)) from e
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise UnsafeUrl(f"ip not public: {literal}")
        return ConnectTarget(url, dial, (str(literal),))
    loop = asyncio.get_running_loop()
    try:
        fn: Resolver = resolver or DEFAULT_RESOLVER or loop.getaddrinfo
        infos = await fn(dial, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as e:
        raise FetchError(f"dns failure for {host}: {e}") from e
    if not infos:
        raise FetchError(f"dns returned nothing for {host}")
    addresses: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            raise UnsafeUrl(f"{host} resolves to non-public {ip}")
        if info[4][0] not in addresses:
            addresses.append(info[4][0])
    return ConnectTarget(url, dial, tuple(addresses))


async def check_url(url: str, resolver: Resolver | None = None) -> str:
    """Validate scheme/host and resolve DNS; return the normalised URL or raise UnsafeUrl.
    Malformed URLs (bad port, invalid IDNA label, ...) are UnsafeUrl too, never a bare
    ValueError/UnicodeError escaping to the caller."""
    try:
        await prepare_connect(url, resolver)
        return url
    except (UnsafeUrl, FetchError):
        raise
    except (ValueError, UnicodeError) as e:
        raise UnsafeUrl(f"malformed url: {type(e).__name__}: {e}"[:200]) from e


@contextlib.asynccontextmanager
async def _stream_request(
    client: httpx.AsyncClient,
    method: str,
    target: ConnectTarget,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> AsyncIterator[httpx.Response]:
    # dev insecure mode validates nothing, so there is nothing to pin
    pin = _pins.set({_pin_key(target.host): target.addresses}) if target.addresses else None
    try:
        async with client.stream(
            method,
            target.url,
            content=content,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        ) as r:
            yield r
    finally:
        if pin is not None:
            _pins.reset(pin)


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
        target = await prepare_connect(current, resolver=resolver)
        try:
            async with _stream_request(
                client,
                "GET",
                target,
                timeout=timeout,
                headers={"Accept": accept, "Accept-Encoding": "identity"},
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
                # We asked for identity. A compressed answer would be inflated by httpx
                # before the cap below sees it (a 64 KB gzip bomb is 66 MB in one chunk):
                # a host that ignores Accept-Encoding is hostile or broken, so refuse it.
                encoding = r.headers.get("content-encoding", "").strip().lower()
                if encoding and encoding != "identity":
                    raise FetchError(
                        f"content-encoding not allowed: {encoding[:20]}", 200, retryable=False
                    )
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
