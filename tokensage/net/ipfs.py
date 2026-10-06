"""IPFS URL handling: CID extraction, dead-gateway rewriting, and racing gateway fallback."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from tokensage.net.safe_fetch import Fetched, FetchError, UnsafeUrl, safe_get

_CID = (
    r"(?P<cid>Qm[1-9A-HJ-NP-Za-km-z]{44,}|b[a-z2-7]{50,}|z[1-9A-HJ-NP-Za-km-z]{40,}|k[a-z0-9]{50,})"
)
_PATH_RE = re.compile(r"/ipfs/" + _CID + r"(?P<rest>/[^?#]*)?", re.I)
_SUBDOMAIN_RE = re.compile(r"^https?://" + _CID + r"\.ipfs\.[^/]+(?P<rest>/[^?#]*)?", re.I)
_SCHEME_RE = re.compile(r"^ipfs://(?:ipfs/)?" + _CID + r"(?P<rest>/[^?#]*)?", re.I)


@dataclass(frozen=True)
class IpfsRef:
    cid: str
    path: str = ""  # "" or "/sub/path"

    @property
    def key(self) -> str:
        return self.cid + self.path


def parse_ipfs(url: str) -> IpfsRef | None:
    """Return the CID (+path) if `url` points at IPFS content in any common form."""
    u = url.strip()
    for rx in (_SCHEME_RE, _SUBDOMAIN_RE, _PATH_RE):
        m = rx.search(u)
        if m:
            return IpfsRef(m.group("cid"), (m.group("rest") or "").rstrip("/"))
    return None


def gateway_urls(ref: IpfsRef, gateways: Sequence[str]) -> list[str]:
    return [f"{g.rstrip('/')}/ipfs/{ref.cid}{ref.path}" for g in gateways]


async def fetch_ipfs(
    client: httpx.AsyncClient,
    ref: IpfsRef,
    gateways: Sequence[str],
    *,
    max_bytes: int,
    timeout: float,
    stagger_s: float = 1.5,
    accept: str = "*/*",
) -> Fetched:
    """Try gateways in order, starting the next one after `stagger_s` while the previous is
    still running. First success wins; the rest are cancelled."""
    urls = gateway_urls(ref, gateways)
    if not urls:
        raise FetchError("no gateways configured", retryable=False)
    errors: list[str] = []
    tasks: list[asyncio.Task[Fetched]] = []

    async def one(u: str) -> Fetched:
        return await safe_get(client, u, max_bytes=max_bytes, timeout=timeout, accept=accept)

    try:
        idx = 0
        pending: set[asyncio.Task[Fetched]] = set()
        while idx < len(urls) or pending:
            if idx < len(urls):
                t = asyncio.create_task(one(urls[idx]))
                tasks.append(t)
                pending.add(t)
                idx += 1
            done, pending = await asyncio.wait(
                pending,
                timeout=stagger_s if idx < len(urls) else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                try:
                    return t.result()
                except (FetchError, UnsafeUrl) as e:
                    errors.append(str(e))
                    # a definitive "not found" at one gateway is not definitive for IPFS
        raise FetchError("all gateways failed: " + "; ".join(errors)[:500], retryable=True)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
