"""Logo downloads are shared: one in flight per file, and its outcome kept briefly, so the
several reads of one coin do not each race the gateways for the same logo. No DB needed."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from tests.fixtures.chain import CID_IMG, GW1, GW2, PNG, public_resolver
from tokensage.config import Settings
from tokensage.net import safe_fetch
from tokensage.resolve import metadata as md


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_fetch, "DEFAULT_RESOLVER", public_resolver)


def _settings() -> Settings:
    return Settings(ipfs_gateways=f"{GW1},{GW2}", _env_file=None)  # type: ignore[call-arg]


async def test_concurrent_reads_share_one_download() -> None:
    calls: list[str] = []

    async def img(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf".*/ipfs/{CID_IMG}$").mock(side_effect=img)
        async with httpx.AsyncClient() as client:
            # the same CID through two different gateways' URLs is the same file
            ms = [
                md.Metadata(status="ok", image_url=f"https://ipfs.io/ipfs/{CID_IMG}"),
                md.Metadata(status="ok", image_url=f"https://dweb.link/ipfs/{CID_IMG}"),
                md.Metadata(status="ok", image_url=f"https://ipfs.io/ipfs/{CID_IMG}"),
            ]
            await asyncio.gather(*(md.attach_image(client, m, _settings()) for m in ms))
            later = md.Metadata(status="ok", image_url=f"https://ipfs.io/ipfs/{CID_IMG}")
            await md.attach_image(client, later, _settings())
    assert len(calls) == 1
    for m in [*ms, later]:
        assert m.image_bytes == PNG and m.image_content_key == f"ipfs:{CID_IMG}"


async def test_a_failure_is_kept_briefly_then_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    ok = False

    def img(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if ok:
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    url = "https://cdn.example.test/logo.png"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(side_effect=img)
        async with httpx.AsyncClient() as client:
            first = md.Metadata(status="ok", image_url=url)
            await md.attach_image(client, first, _settings())
            again = md.Metadata(status="ok", image_url=url)
            await md.attach_image(client, again, _settings())
            assert len(calls) == 1  # the dead link is not asked again straight away
            assert first.image_error and again.image_error == first.image_error
            assert again.image_content_key is None

            # once the failure has aged out, the next read tries again
            monkeypatch.setattr(md, "IMAGE_MEMO_FAIL_S", 0.0)
            md.image_memo.clear()
            ok = True
            third = md.Metadata(status="ok", image_url=url)
            await md.attach_image(client, third, _settings())
    assert len(calls) == 2
    assert third.image_bytes == PNG and third.image_content_key


async def test_memo_is_bounded_by_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(md, "IMAGE_MEMO_MAX_BYTES", len(PNG) * 2)
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(cdn\.example\.test|93\.184\.216\.34)/\d+\.png").mock(
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        )
        async with httpx.AsyncClient() as client:
            for i in range(5):
                m = md.Metadata(status="ok", image_url=f"https://cdn.example.test/{i}.png")
                await md.attach_image(client, m, _settings())
    assert len(md.image_memo.done) == 2
    assert md.image_memo.bytes == len(PNG) * 2
