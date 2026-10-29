"""The pump.fun relay (sources/pumpfun.py, GET /v1/pumpfun/coins/{ca}): check-in 2026-10-29."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from tests.conftest import needs_db
from tokensage.sources import pumpfun

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
URL = pumpfun.URL.format(mint=MINT)


@pytest.fixture(autouse=True)
def _fresh_relay() -> Iterator[None]:
    pumpfun.relay.clear()
    yield
    pumpfun.relay.clear()


def _coin(**extra: object) -> dict[str, object]:
    return {"mint": MINT, "name": "Blorbo", "symbol": "BLORBO", **extra}


def test_clean_coin_keeps_whitelisted_scalars_only() -> None:
    raw = _coin(
        description="x" * 5000,
        twitter="https://x.com/blorbo",
        created_timestamp=1_793_000_000_000,
        complete=False,
        is_currently_live="yes",  # not a bool: dropped
        usd_market_cap=True,  # a bool is not a number
        creator={"nested": 1},
        raydium_pool="anything",  # not whitelisted
    )
    got = pumpfun.clean_coin(raw, MINT)
    assert got is not None
    assert got["description"] == "x" * 2000
    assert got["created_timestamp"] == 1_793_000_000_000 and got["complete"] is False
    assert "is_currently_live" not in got and "usd_market_cap" not in got
    assert "creator" not in got and "raydium_pool" not in got
    assert pumpfun.clean_coin(_coin(mint="other"), MINT) is None
    assert pumpfun.clean_coin(["not", "a", "dict"], MINT) is None


@respx.mock
async def test_an_answer_is_relayed_and_cached() -> None:
    route = respx.get(URL).respond(200, json=_coin(website="https://blorbo.fun"))
    async with httpx.AsyncClient() as http:
        first = await pumpfun.coin(http, MINT)
        second = await pumpfun.coin(http, MINT)
    assert first.status == "ok" and first.coin and first.coin["website"] == "https://blorbo.fun"
    assert route.calls.last.request.headers["origin"] == "https://pump.fun"
    assert second.cached and second.coin == first.coin
    assert route.call_count == 1


@respx.mock
async def test_a_refusal_says_so_and_holds_the_route() -> None:
    route = respx.get(URL).respond(
        403, headers={"cf-mitigated": "challenge"}, text="<title>Just a moment...</title>"
    )
    async with httpx.AsyncClient() as http:
        first = await pumpfun.coin(http, MINT)
        second = await pumpfun.coin(http, MINT)
    assert (first.status, first.upstream_status, first.cf_mitigated) == (
        "refused",
        403,
        "challenge",
    )
    # held back: the same refusal, no second call
    assert (second.status, second.cf_mitigated) == ("refused", "challenge")
    assert route.call_count == 1


@respx.mock
async def test_the_shared_cap_answers_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pumpfun, "MAX_PER_MIN", 2)
    respx.get(url__regex=r"https://frontend-api-v3\.pump\.fun/coins-v2/.*").respond(404)
    mints = [MINT[:-1] + c for c in "abc"]
    async with httpx.AsyncClient() as http:
        answers = [await pumpfun.coin(http, m) for m in mints]
    assert [a.status for a in answers] == ["not_found", "not_found", "busy"]


@respx.mock
async def test_unavailable_is_not_cached() -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=_coin())]
    )
    async with httpx.AsyncClient() as http:
        first = await pumpfun.coin(http, MINT)
        second = await pumpfun.coin(http, MINT)
    assert (first.status, first.upstream_status) == ("unavailable", 502)
    assert second.status == "ok" and not second.cached
    assert route.call_count == 2


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@needs_db
async def test_route_relays_a_coin(router: respx.MockRouter, client: httpx.AsyncClient) -> None:
    router.get(URL).respond(200, json=_coin(description="a frog"))
    r = await client.get(f"/v1/pumpfun/coins/{MINT}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["coin"]["description"] == "a frog"
    assert body["coin"]["mint"] == MINT and body["request_id"]
    assert (await client.get("/v1/pumpfun/coins/not-a-mint")).status_code == 400
    assert (
        await client.get(f"/v1/pumpfun/coins/{MINT}", headers={"Authorization": "Bearer nope"})
    ).status_code == 401
