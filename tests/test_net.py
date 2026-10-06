"""safe_fetch, IPFS parsing/fallback, metadata cleaning, Metaplex roundtrip. No DB needed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from tests.fixtures.chain import GW1, GW2, public_resolver
from tokensage.net import safe_fetch
from tokensage.net.ipfs import IpfsRef, fetch_ipfs, parse_ipfs
from tokensage.net.safe_fetch import FetchError, UnsafeUrl, check_url, safe_get
from tokensage.resolve import metaplex
from tokensage.resolve.metadata import build, clean_social, clean_url, rewrite_dead_gateways


async def private_resolver(host: str, port: int, **_: Any) -> list[Any]:
    return [(2, 1, 6, "", ("10.0.0.5", port))]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


# ------------------------------------------------------------- check_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://[::ffff:10.0.0.1]/x",
        "https://metadata.google.internal/",
        "https://user:pw@example.com/",
    ],
)
async def test_check_url_rejects(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        await check_url(url)


async def test_check_url_rejects_private_dns() -> None:
    with pytest.raises(UnsafeUrl):
        await check_url("https://evil.test/", resolver=private_resolver)


async def test_check_url_accepts_public() -> None:
    assert await check_url("https://ok.test/a") == "https://ok.test/a"
    assert await check_url("https://93.184.216.34/a")


# ------------------------------------------------------------- safe_get


@respx.mock
async def test_safe_get_follows_safe_redirects_and_rechecks() -> None:
    respx.get("https://a.test/1").mock(return_value=httpx.Response(302, headers={"location": "/2"}))
    respx.get("https://a.test/2").mock(return_value=httpx.Response(200, content=b"ok"))
    async with httpx.AsyncClient() as c:
        f = await safe_get(c, "https://a.test/1", max_bytes=100, timeout=5)
    assert f.body == b"ok" and f.url == "https://a.test/2"


@respx.mock
async def test_safe_get_blocks_redirect_to_private() -> None:
    respx.get("https://a.test/1").mock(
        return_value=httpx.Response(302, headers={"location": "https://127.0.0.1/"})
    )
    async with httpx.AsyncClient() as c:
        with pytest.raises(UnsafeUrl):
            await safe_get(c, "https://a.test/1", max_bytes=100, timeout=5)


@respx.mock
async def test_safe_get_caps_size_and_redirect_count() -> None:
    respx.get("https://a.test/big").mock(return_value=httpx.Response(200, content=b"x" * 1000))
    for i in range(6):
        respx.get(f"https://a.test/r{i}").mock(
            return_value=httpx.Response(302, headers={"location": f"/r{i + 1}"})
        )
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError, match="too large"):
            await safe_get(c, "https://a.test/big", max_bytes=100, timeout=5)
        with pytest.raises(FetchError, match="too many redirects"):
            await safe_get(c, "https://a.test/r0", max_bytes=100, timeout=5)


@respx.mock
async def test_safe_get_status_classification() -> None:
    respx.get("https://a.test/404").mock(return_value=httpx.Response(404))
    respx.get("https://a.test/503").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError) as e1:
            await safe_get(c, "https://a.test/404", max_bytes=10, timeout=5)
        assert e1.value.retryable is False
        with pytest.raises(FetchError) as e2:
            await safe_get(c, "https://a.test/503", max_bytes=10, timeout=5)
        assert e2.value.retryable is True


# ------------------------------------------------------------- ipfs


@pytest.mark.parametrize(
    "url,cid,path",
    [
        (
            "https://ipfs.io/ipfs/bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
            "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
            "",
        ),
        (
            "ipfs://QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG/meta.json",
            "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",
            "/meta.json",
        ),
        (
            "https://bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe.ipfs.dweb.link/",
            "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
            "",
        ),
        (
            "https://cf-ipfs.com/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG?x=1",
            "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",
            "",
        ),
    ],
)
def test_parse_ipfs(url: str, cid: str, path: str) -> None:
    assert parse_ipfs(url) == IpfsRef(cid, path)


def test_parse_ipfs_non_ipfs() -> None:
    assert parse_ipfs("https://metadata.j7tracker.io/m/BYoLJHtQCb") is None
    assert parse_ipfs("https://meta.uxento.io/data/714b0bec-90cb-4cbb-9598-b7bcbcd3bd06") is None


@respx.mock
async def test_fetch_ipfs_falls_back_to_second_gateway() -> None:
    cid = "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe"
    respx.get(f"{GW1}/ipfs/{cid}").mock(return_value=httpx.Response(429))
    respx.get(f"{GW2}/ipfs/{cid}").mock(return_value=httpx.Response(200, content=b"{}"))
    async with httpx.AsyncClient() as c:
        f = await fetch_ipfs(c, IpfsRef(cid), [GW1, GW2], max_bytes=100, timeout=5, stagger_s=0.01)
    assert f.body == b"{}" and f.url.startswith(GW2)


@respx.mock
async def test_fetch_ipfs_all_fail_is_retryable() -> None:
    cid = "bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe"
    respx.get(url__regex=r"https://gw[12]\.test/.*").mock(return_value=httpx.Response(504))
    async with httpx.AsyncClient() as c:
        with pytest.raises(FetchError) as e:
            await fetch_ipfs(c, IpfsRef(cid), [GW1, GW2], max_bytes=100, timeout=5, stagger_s=0.01)
    assert e.value.retryable


# ------------------------------------------------------------- metadata cleaning


def test_clean_url_and_social() -> None:
    assert clean_url("javascript:alert(1)") is None
    assert clean_url("data:text/html,x") is None
    assert clean_url("http://example.com/a") == "https://example.com/a"
    assert clean_url("x.com/foo") == "https://x.com/foo"
    assert clean_url("") is None and clean_url(None) is None and clean_url(123) is None
    assert clean_social("@pumpdotfun") == "@pumpdotfun"
    assert clean_social("https://x.com/i/communities/1804846498066116981") is not None
    assert clean_social("<script>") is None


def test_rewrite_dead_gateways() -> None:
    assert rewrite_dead_gateways("https://cf-ipfs.com/ipfs/Qm1") == "https://ipfs.io/ipfs/Qm1"
    assert (
        rewrite_dead_gateways("https://cloudflare-ipfs.com/ipfs/Qm1") == "https://ipfs.io/ipfs/Qm1"
    )


def test_build_metadata_tolerates_junk() -> None:
    m = build(
        "https://ipfs.io/ipfs/bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
        b'{"name": 123, "symbol": null, "description": "d\\u0000x", "image": "javascript:x",'
        b' "twitter": ["a"], "website": "http://w.test"}',
    )
    assert m.status == "ok" and m.name == "123" and m.symbol is None
    assert m.description == "dx" and m.image_url is None
    assert m.website == "https://w.test" and m.twitter is None
    assert m.content_key and m.content_key.startswith("ipfs:")
    bad = build("https://x.test/m", b"not json")
    assert bad.status == "invalid" and bad.content_key and bad.content_key.startswith("sha256:")
    arr = build("https://x.test/m", b"[1,2]")
    assert arr.status == "invalid"


# ------------------------------------------------------------- metaplex


def test_metaplex_roundtrip_and_pda() -> None:
    data = metaplex.encode_metadata_for_tests(
        "Peanut the Squirrel", "PNUT", "https://ipfs.io/ipfs/Qm1"
    )
    d = metaplex.decode_metadata(data)
    assert d == {"name": "Peanut the Squirrel", "symbol": "PNUT", "uri": "https://ipfs.io/ipfs/Qm1"}
    pda = metaplex.metadata_pda("3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump")
    assert len(pda) in (43, 44)
    with pytest.raises(ValueError):
        metaplex.decode_metadata(b"\x00" * 10)
