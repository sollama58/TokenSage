"""Full audit 2026-11-02 (TrenchScanner project thread): TokenSage findings."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from tokensage.net import breaker as breaker_mod
from tokensage.sources import gnews

# ------------------------------------------------------------- TS-1


@pytest.fixture
def fresh_breaker(monkeypatch: pytest.MonkeyPatch) -> breaker_mod.CircuitBreaker:
    b = breaker_mod.CircuitBreaker(threshold=2, cooldown_s=0.05)
    monkeypatch.setattr(gnews, "breaker", b)
    return b


@respx.mock
async def test_gnews_failures_open_the_breaker_and_one_caller_probes_after_the_cooldown(
    fresh_breaker: breaker_mod.CircuitBreaker,
) -> None:
    route = respx.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text="<html><body>Before you continue to Google</body>")
    )
    async with httpx.AsyncClient() as http:
        assert await gnews.search(http, "Moo Deng") is None
        assert await gnews.search(http, "Moo Deng") is None  # the second failure opens it
        assert not fresh_breaker.allow("gnews")
        # Open: nobody goes out.
        assert await gnews.search(http, "Moo Deng") is None
        assert route.call_count == 2
        time.sleep(0.06)
        # Past the cooldown, the callers that arrive together: one probes, the rest skip at
        # once, in no time, instead of every one waiting out the timeout.
        started = time.monotonic()
        answers = await asyncio.gather(*(gnews.search(http, "Moo Deng") for _ in range(5)))
        assert answers == [None] * 5
        assert route.call_count == 3
        assert time.monotonic() - started < gnews.TIMEOUT_S
        # The probe failed, so the breaker is open for a full cooldown again.
        assert not fresh_breaker.allow("gnews")


@respx.mock
async def test_gnews_times_out_within_its_own_budget(
    fresh_breaker: breaker_mod.CircuitBreaker,
) -> None:
    assert gnews.TIMEOUT_S <= 4.0

    async def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    respx.get(url__startswith="https://news.google.com/rss/search").mock(side_effect=slow)
    async with httpx.AsyncClient() as http:
        assert await gnews.search(http, "Moo Deng") is None
    assert fresh_breaker.states["gnews"].failures == 1


@respx.mock
async def test_gnews_success_closes_the_breaker(fresh_breaker: breaker_mod.CircuitBreaker) -> None:
    fresh_breaker.failure("gnews")
    rss = (
        "<rss><channel><item><title>Moo Deng turns one</title>"
        "<source url='x'>BBC</source><pubDate>Mon</pubDate></item></channel></rss>"
    )
    respx.get(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, text=rss)
    )
    async with httpx.AsyncClient() as http:
        heads = await gnews.search(http, "Moo Deng")
    assert heads is not None and heads[0].title == "Moo Deng turns one"
    assert "gnews" not in fresh_breaker.states
