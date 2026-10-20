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
def _no_upstream_cooldowns() -> None:
    """A 429 a previous test provoked must not leave an IPFS gateway or GeckoTerminal cooling
    down for the next."""
    from tokensage.net import ipfs
    from tokensage.sources import geckoterminal

    ipfs._gateway_cooldown.clear()
    geckoterminal._cooldown_until = 0.0


def _should_pin_respx_url(
    url: str | None, kwargs: dict[str, object], *, pin_ips: tuple[str, ...]
) -> bool:
    return bool(
        isinstance(url, str)
        and url.startswith("https://")
        and not any(url.startswith(f"https://{ip}") for ip in pin_ips)
        and "url__regex" not in kwargs
        and "url__startswith" not in kwargs
    )


def _mirror_respx_mock(primary: object, secondary: object) -> None:
    """Apply the same .mock(...) to both hostname and pinned-IP routes."""
    orig_mock = primary.mock  # type: ignore[attr-defined]

    def mock(*args: object, **kwargs: object):
        orig_mock(*args, **kwargs)
        secondary.mock(*args, **kwargs)  # type: ignore[attr-defined]
        return primary

    primary.mock = mock  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _respx_dns_pin_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """safe_fetch connects to pinned IPs; mirror plain https URL mocks for the pinned URL too."""
    import respx

    from tests.fixtures.chain import _PIN_BY_HOST, PUBLIC_PIN_IP, pinned_url

    pin_ips = (*_PIN_BY_HOST.values(), PUBLIC_PIN_IP)
    orig = respx.get

    def get(url: str | None = None, *args: object, **kwargs: object):
        route = orig(url, *args, **kwargs) if url is not None else orig(*args, **kwargs)
        if _should_pin_respx_url(url, kwargs, pin_ips=pin_ips):
            twin = orig(pinned_url(url), *args, **kwargs)
            _mirror_respx_mock(route, twin)
        return route

    monkeypatch.setattr(respx, "get", get)

    orig_router_get = respx.MockRouter.get
    orig_router_post = respx.MockRouter.post

    def router_get(self, url: str | None = None, *args: object, **kwargs: object):
        route = orig_router_get(self, url, *args, **kwargs)
        if _should_pin_respx_url(url, kwargs, pin_ips=pin_ips):
            twin = orig_router_get(self, pinned_url(url), *args, **kwargs)
            _mirror_respx_mock(route, twin)
        return route

    def router_post(self, url: str | None = None, *args: object, **kwargs: object):
        route = orig_router_post(self, url, *args, **kwargs)
        if _should_pin_respx_url(url, kwargs, pin_ips=pin_ips):
            twin = orig_router_post(self, pinned_url(url), *args, **kwargs)
            _mirror_respx_mock(route, twin)
        return route

    monkeypatch.setattr(respx.MockRouter, "get", router_get)
    monkeypatch.setattr(respx.MockRouter, "post", router_post)


@pytest.fixture(autouse=True)
def _empty_image_memo() -> None:
    """Every test downloads its own logos: a previous test's shared download (or its failure)
    must not stand in for the fetch a test counts."""
    from tokensage.resolve.metadata import image_memo

    image_memo.clear()


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
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def clean_tables(migrated_db: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            "truncate job, analysis, token, api_usage, api_key, pair_token restart identity cascade"
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
