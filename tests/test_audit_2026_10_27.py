"""Regression tests for the 2026-10-27 full audit (TokenSage lane): GeckoTerminal call
spacing (TS-5), 404 answers on the upstream panel (TS-6) and migrations taking turns (DB-5).
The meta-count cache (TS-1), the stage timings (TS-2, TS-3), the failed story heat (TS-4) and
the database size alarm (DB-4) are pinned beside the tests of the code they fix."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tokensage.sources import geckoterminal


async def test_geckoterminal_calls_are_spaced_not_sent_in_a_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAT_BURST calls go back to back (a search and its peak); the next waits for its turn
    when that is close, and is RateLimited without a call when it is not. Ten calls in one
    second used to go out together and draw 429s together."""
    monkeypatch.setattr(geckoterminal, "MIN_GAP_S", 0.3)
    monkeypatch.setattr(geckoterminal, "MAX_WAIT_S", 0.2)
    monkeypatch.setattr(geckoterminal, "HEAT_BURST", 2)
    geckoterminal._calls.clear()
    with respx.mock() as router:
        route = router.get(url__startswith=geckoterminal.BASE).mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async with httpx.AsyncClient() as http:
            started = time.monotonic()
            await geckoterminal._get(http, "/search/pools", {})
            await geckoterminal._get(http, "/search/pools", {})
            assert time.monotonic() - started < 0.15  # the burst allowance: no wait
            # the third's turn is 0.3 s away, past MAX_WAIT_S: no call goes out
            with pytest.raises(geckoterminal.RateLimited):
                await geckoterminal._get(http, "/search/pools", {})
            assert route.call_count == 2
            # 0.15 s on, its turn is 0.15 s away: it waits for it instead of failing
            time.sleep(0.15)
            waited_from = time.monotonic()
            await geckoterminal._get(http, "/search/pools", {})
            assert time.monotonic() - waited_from >= 0.1
            assert route.call_count == 3
    geckoterminal._calls.clear()


async def test_a_404_from_an_x_mirror_or_wikimedia_is_an_answer_not_an_error() -> None:
    """oEmbed is asked only when no mirror found a live copy, so its answers are mostly 404
    (deleted); Wikimedia 404s a day not published yet. Both read as dead upstreams on the
    panel when a 404 counted as an error (TS-6). A 404 elsewhere, and a 403 anywhere, still
    count."""
    from tokensage.net import metrics

    def answer(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403 if req.url.path == "/blocked" else 404)

    metrics.meter.take()
    transport = metrics.MeteredTransport(httpx.MockTransport(answer))
    async with httpx.AsyncClient(transport=transport) as c:
        await c.get("https://publish.x.com/oembed")
        await c.get("https://wikimedia.org/api/rest_v1/metrics/pageviews/top")
        await c.get("https://publish.x.com/blocked")
        await c.get("https://api.dexscreener.com/latest/x")
    errors: dict[str, int] = {}
    for (_, source, _), counter in metrics.meter.take().items():
        errors[source] = errors.get(source, 0) + counter.errors
    assert errors == {"x_oembed": 1, "wikimedia": 0, "dexscreener": 1}


@needs_db
async def test_two_migration_runs_take_turns(migrated_db: str) -> None:
    """The analyzer now migrates before it starts, as the api does, so a deploy runs two
    upgrades at once (DB-5). The second waits for the first's lock and then finds the
    database at head, instead of applying the same migration twice."""
    db = await asyncpg.connect(migrated_db)
    try:
        await db.execute("select pg_advisory_lock(hashtext('tokensage:migrations'))")
        env = {**os.environ, "DATABASE_URL": migrated_db}
        run = subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            await asyncio.sleep(2.0)
            assert run.poll() is None  # waiting on the lock this test holds
            await db.execute("select pg_advisory_unlock(hashtext('tokensage:migrations'))")
            _, err = await asyncio.to_thread(run.communicate, timeout=60)
            assert run.returncode == 0, err.decode()[-500:]
        finally:
            if run.poll() is None:
                run.kill()
        # it let go: the next run gets the lock at once
        assert await db.fetchval("select pg_try_advisory_lock(hashtext('tokensage:migrations'))")
        await db.execute("select pg_advisory_unlock(hashtext('tokensage:migrations'))")
    finally:
        await db.close()
