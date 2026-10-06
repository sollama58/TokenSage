"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tokensage import __version__, queue
from tokensage.api import errors
from tokensage.api.auth import KeyStore, require_admin
from tokensage.api.routes_v1 import router as v1
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.keys = KeyStore(settings)
        app.state.pool = await create_pool(settings.database_url)
        app.state.waiter = queue.DoneWaiter(app.state.pool)
        await app.state.waiter.start()
        stop = asyncio.Event()
        inline: asyncio.Task[None] | None = None
        if settings.inline_analyzer:
            from tokensage.worker import run_worker

            inline = asyncio.create_task(run_worker(app.state.pool, settings, stop))
            log.info("inline_analyzer.started")
        if not settings.api_key_pairs:
            log.warning("auth.no_api_keys_configured")
        try:
            yield
        finally:
            stop.set()
            if inline is not None:
                await inline
            await app.state.waiter.stop()
            await app.state.pool.close()

    app = FastAPI(
        title="TokenSage",
        version=__version__,
        description=(
            "Explains what a pump.fun token means, given its contract address. "
            "Deterministic analysis of name, ticker, image and linked X/Twitter content."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            resp = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        resp.headers["X-Request-Id"] = rid
        return resp

    app.add_exception_handler(errors.ApiError, errors.api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, errors.http_error_handler)  # type: ignore[arg-type]

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def readyz(request: Request) -> JSONResponse:
        pool = request.app.state.pool
        async with pool.acquire() as conn:
            pending = await queue.pending_count(conn)
            running = await conn.fetchval("select count(*) from job where status='running'")
            failed_24h = await conn.fetchval(
                """select count(*) from job where status='failed'
                   and finished_at > now() - interval '1 day'"""
            )
            health = await conn.fetch(
                "select source, state, failures, open_until from source_health"
            )
        return JSONResponse(
            {
                "status": "ok",
                "queue": {"pending": pending, "running": running, "failed_24h": failed_24h},
                "sources": [dict(r) for r in health],
                "inline_analyzer": settings.inline_analyzer,
            }
        )

    app.include_router(v1)
    return app


app = create_app()
