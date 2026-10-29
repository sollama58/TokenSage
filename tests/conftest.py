"""Shared fixtures. Integration tests need a Postgres at TEST_DATABASE_URL (or DATABASE_URL);
they are skipped when none is reachable, unless TS_REQUIRE_DB=1 (CI), which then fails the
run instead of passing on unit tests alone."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest

from tokensage.config import Settings, normalize_database_url

TEST_DB = normalize_database_url(
    os.environ.get("TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or "postgresql://tokensage@localhost:5432/tokensage"
)
API_KEY = "test-key-0123456789abcdef"
ADMIN_KEY = "admin-key-0123456789abcdef"
MONITOR_KEY = "monitor-key-0123456789abcdef"


def _db_reachable() -> bool:
    import asyncio

    async def probe() -> bool:
        try:
            c = await asyncpg.connect(TEST_DB, timeout=3)
            await c.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    return asyncio.run(probe())


DB_AVAILABLE = _db_reachable()
needs_db = pytest.mark.skipif(not DB_AVAILABLE, reason="no test Postgres reachable")
REQUIRE_DB = os.environ.get("TS_REQUIRE_DB", "") == "1"


def pytest_configure(config: pytest.Config) -> None:
    if REQUIRE_DB and not DB_AVAILABLE:
        pytest.exit(
            f"TS_REQUIRE_DB=1 but no Postgres is reachable at {TEST_DB}: the DB-backed tests "
            "would be skipped",
            returncode=1,
        )


@pytest.fixture(autouse=True)
def _closed_breakers() -> None:
    """Every test starts with all circuit breakers closed: failures a previous test provoked
    (an unmocked source hitting a catch-all 404) must not turn a source off for the next."""
    from tokensage.net.breaker import breaker

    breaker.states.clear()


@pytest.fixture(autouse=True)
def _no_upstream_cooldowns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 a previous test provoked must not leave an IPFS gateway or GeckoTerminal cooling
    down for the next. GeckoTerminal's call spacing is off unless a test turns it on: a test's
    mocked calls would otherwise wait MIN_GAP_S apart."""
    from tokensage.net import ipfs
    from tokensage.sources import geckoterminal

    ipfs._gateway_cooldown.clear()
    geckoterminal._cooldown_until = 0.0
    geckoterminal._next_at = 0.0
    geckoterminal._refusals = 0
    monkeypatch.setattr(geckoterminal, "MIN_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def _empty_image_memo() -> None:
    """Every test downloads its own logos: a previous test's shared download (or its failure)
    must not stand in for the fetch a test counts."""
    from tokensage.resolve.metadata import image_memo

    image_memo.clear()


@pytest.fixture(autouse=True)
def _empty_search_memo() -> None:
    """Every test asks DexScreener itself: a previous test's cached name search must not
    stand in for the one a test mocks."""
    from tokensage.sources.lookups import search_memo

    search_memo.clear()


@pytest.fixture(autouse=True)
def _empty_heat_fallback() -> None:
    """A previous test's failed story-heat refresh must not stand in for the one a test mocks."""
    from tokensage import now

    now._heat_fallback.clear()


@pytest.fixture(autouse=True)
def _empty_meta_counts_cache() -> None:
    """Every test counts the tokens it inserted itself: a previous test's cached current-meta
    counts (same launch slot, same words) must not stand in for them."""
    from tokensage import analyzer

    analyzer.reset_meta_counts_cache()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not DB_AVAILABLE:
        pytest.skip("no test Postgres reachable")
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        check=True,
        capture_output=True,
    )
    return TEST_DB


@pytest.fixture
def settings(migrated_db: str) -> Settings:
    return Settings(
        database_url=migrated_db,
        api_keys=f"tester:{API_KEY}",
        admin_key=ADMIN_KEY,
        monitor_key=MONITOR_KEY,
        inline_analyzer=True,
        default_wait_s=5,
        solana_rpc_url="https://rpc.test/",
        ipfs_gateways="https://gw1.test,https://gw2.test",
        worker_poll_interval_s=0.2,
        enable_basic_x_fetch=False,  # integration tests: no X mirror calls unless overridden
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def clean_tables(migrated_db: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            "truncate job, analysis, token, api_usage, api_key, pair_token, "
            "x_tweet, x_profile restart identity cascade"
        )
        yield
    finally:
        await conn.close()


@pytest.fixture
async def client(settings: Settings, clean_tables: None) -> AsyncIterator[httpx.AsyncClient]:
    from tokensage.api.app import create_app

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as c,
    ):
        yield c
