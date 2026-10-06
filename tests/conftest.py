"""Shared fixtures. Integration tests need a Postgres at TEST_DATABASE_URL (or DATABASE_URL);
they are skipped when none is reachable."""

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
        inline_analyzer=True,
        default_wait_s=5,
        worker_poll_interval_s=0.2,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
async def clean_tables(migrated_db: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate job, analysis, token, api_usage restart identity cascade")
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
