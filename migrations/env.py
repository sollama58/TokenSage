"""Alembic environment. Migrations are plain SQL run through SQLAlchemy + psycopg (sync)."""

from __future__ import annotations

import sys
import time
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from tokensage.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _sync_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


# A database created in the same Blueprint sync can still be starting when the pre-deploy
# command runs ("connection refused"). Wait for it instead of failing the deploy.
CONNECT_WAIT_S = 180.0
CONNECT_RETRY_S = 5.0


def _connect_with_retry(engine: Engine) -> Connection:
    deadline = time.monotonic() + CONNECT_WAIT_S
    while True:
        try:
            return engine.connect()
        except OperationalError as e:
            if time.monotonic() >= deadline:
                raise
            reason = str(e.orig).splitlines()[0] if e.orig else str(e)
            print(
                f"database not ready ({reason}); retrying in {CONNECT_RETRY_S:.0f}s",
                file=sys.stderr,
            )
            time.sleep(CONNECT_RETRY_S)


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    with _connect_with_retry(engine) as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
