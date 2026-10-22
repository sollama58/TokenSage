"""Regression tests for the 2026-10-22 audit: each reproduces a verified bug."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import asyncpg
import httpcore
import httpx
import pytest
import respx

from tests.conftest import API_KEY, needs_db
from tests.fixtures.chain import CID_META, T22_MINT, FakeChain, install_web
from tests.test_phase5_integration import make_client, router  # noqa: F401
from tokensage.api.schemas import TokenResponse
from tokensage.net import metrics, safe_fetch
from tokensage.net.safe_fetch import PinnedTransport, safe_get

# ------------------------------------------------- TS-1: a trusted key's hinted reads count

HINTS = {
    "name": "dog wif cap",
    "symbol": "cap",
    "description": "description from the caller",
    "twitter": "https://x.com/elonmusk/status/1791351500217754008",
    "created_at": "2026-10-01T12:00:00Z",
}
PEER_KEY = "peer-key-0123456789abcdef"


def _chain() -> FakeChain:
    c = FakeChain()
    c.add_t22_pump(T22_MINT, "dog wif cap", "cap", f"https://ipfs.io/ipfs/{CID_META}")
    return c


async def _shared_rows(db: str) -> tuple[int, int]:
    conn = await asyncpg.connect(db)
    try:
        xref = await conn.fetchval("select count(*) from x_ref where mint=$1", T22_MINT)
        read = await conn.fetchval("select count(*) from token_read where mint=$1", T22_MINT)
    finally:
        await conn.close()
    return xref, read


@needs_db
async def test_trusted_key_hints_write_xref_and_token_read(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    """TrenchScanner hints every read, so with SG-003 alone no coin it read reached x_ref or
    token_read: X-reuse counts and launch waves fell to about zero."""
    install_web(router, _chain())
    keys = f"tester:{API_KEY},peer:{PEER_KEY}"
    async with make_client(migrated_db, api_keys=keys, trusted_hint_keys="tester") as c:
        r = await c.post(f"/v1/tokens/{T22_MINT}", params={"wait": 5}, json={"hints": HINTS})
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.raw.description == "description from the caller"
    assert await _shared_rows(migrated_db) == (1, 1)
    # and the read is an ordinary cache hit for another key
    async with make_client(migrated_db, api_keys=keys, bearer=PEER_KEY) as peer:
        g = await peer.get(f"/v1/tokens/{T22_MINT}", params={"wait": 5})
    assert g.status_code == 200, g.text
    peer_a = TokenResponse.model_validate(g.json()).analysis
    assert peer_a is not None and peer_a.raw.description == "description from the caller"


@needs_db
async def test_untrusted_key_hints_stay_private(
    migrated_db: str,
    clean_tables: None,
    router: respx.MockRouter,  # noqa: F811
) -> None:
    install_web(router, _chain())
    async with make_client(migrated_db, trusted_hint_keys="trenchscanner") as c:
        r = await c.post(f"/v1/tokens/{T22_MINT}", params={"wait": 5}, json={"hints": HINTS})
    assert r.status_code == 200, r.text
    assert await _shared_rows(migrated_db) == (0, 0)


# ------------------------------------------------- TS-2: pin the address, not the URL

SHARED_IP = "104.16.1.1"  # one Cloudflare address in front of several zones
OK = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"


class _Dialer(httpcore.AsyncMockBackend):
    """Answers every request with 200 "ok" and records each address it is asked to dial;
    `dead` addresses refuse the connection."""

    def __init__(self, dead: Iterable[str] = ()) -> None:
        super().__init__([OK, OK, OK])
        self.dialed: list[str] = []
        self.dead = set(dead)

    async def connect_tcp(self, host: str, port: int, *a: Any, **kw: Any) -> Any:
        self.dialed.append(host)
        if host in self.dead:
            raise httpcore.ConnectError(f"refused: {host}")
        return await super().connect_tcp(host, port, *a, **kw)


def _resolver(*ips: str) -> Any:
    async def resolve(host: str, port: int, **_: Any) -> list[Any]:
        return [(2, 1, 6, "", (ip, port)) for ip in ips]

    return resolve


async def test_two_hosts_on_one_address_do_not_share_a_connection() -> None:
    """With the IP in the URL httpcore pooled by IP: a connection opened (and certificate-
    checked) for one host carried the next host's request too, and Cloudflare answered the
    cross-zone SNI/Host mismatch with 403/421."""
    dialer = _Dialer()
    async with httpx.AsyncClient(transport=PinnedTransport(network_backend=dialer)) as c:
        kw: dict[str, Any] = dict(max_bytes=100, timeout=5, resolver=_resolver(SHARED_IP))
        a1 = await safe_get(c, "https://pump.mypinata.cloud/ipfs/a", **kw)
        b = await safe_get(c, "https://gateway.pinata.cloud/ipfs/b", **kw)
        a2 = await safe_get(c, "https://pump.mypinata.cloud/ipfs/c", **kw)
    assert a1.body == b.body == a2.body == b"ok"
    # one connection per host, both to the validated address; the third request reuses the first
    assert dialer.dialed == [SHARED_IP, SHARED_IP]


async def test_pinned_connect_tries_every_validated_address() -> None:
    dialer = _Dialer(dead={"104.16.9.9"})
    async with httpx.AsyncClient(transport=PinnedTransport(network_backend=dialer)) as c:
        f = await safe_get(
            c,
            "https://img.test/logo.png",
            max_bytes=100,
            timeout=5,
            resolver=_resolver("104.16.9.9", "93.184.216.34"),
        )
    assert f.body == b"ok"
    assert dialer.dialed == ["104.16.9.9", "93.184.216.34"]


async def test_pinned_backend_never_resolves_a_guarded_host_again() -> None:
    dialer = _Dialer()
    backend = safe_fetch.PinnedBackend(dialer)
    token = safe_fetch._pins.set({"a.test": ("93.184.216.34",)})
    try:
        with pytest.raises(httpcore.ConnectError, match="no validated address"):
            await backend.connect_tcp("b.test", 443)
        await backend.connect_tcp("a.test", 443)
    finally:
        safe_fetch._pins.reset(token)
    await backend.connect_tcp("rpc.test", 443)  # an unguarded call resolves as usual
    assert dialer.dialed == ["93.184.216.34", "rpc.test"]


async def test_guarded_fetches_are_metered_by_hostname() -> None:
    """With the IP in the URL every IPFS and X media fetch was counted as `other`."""
    saved = metrics.meter.ipfs_hosts
    metrics.meter.ipfs_hosts = {"pump.mypinata.cloud"}
    metrics.meter.take()
    transport = metrics.MeteredTransport(PinnedTransport(network_backend=_Dialer()))
    try:
        async with httpx.AsyncClient(transport=transport) as c:
            await safe_get(
                c,
                "https://pump.mypinata.cloud/ipfs/a",
                max_bytes=100,
                timeout=5,
                resolver=_resolver(SHARED_IP),
            )
        assert {(s, m) for (_, s, m) in metrics.meter.take()} == {("ipfs", "GET")}
    finally:
        metrics.meter.ipfs_hosts = saved


def test_shared_clients_pin_connections() -> None:
    assert isinstance(metrics.MeteredTransport().inner, PinnedTransport)
