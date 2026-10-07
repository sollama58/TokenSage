"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response

from tokensage import __version__, queue
from tokensage.api import errors
from tokensage.api.auth import KeyStore, require_admin
from tokensage.api.routes_admin import router as admin
from tokensage.api.routes_v1 import router as v1
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging

log = structlog.get_logger("api")
KEY_RELOAD_S = 30.0  # how soon another instance's key changes reach this one


async def _reload_keys_forever(app: FastAPI, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=KEY_RELOAD_S)
        except TimeoutError:
            try:
                async with app.state.pool.acquire() as conn:
                    await app.state.keys.reload(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("auth.key_reload_failed", error=f"{type(e).__name__}: {e}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.keys = KeyStore(settings)
        inline_n = max(1, settings.inline_worker_concurrency) if settings.inline_analyzer else 0
        # API requests share the pool with the inline analyzer's job connections
        app.state.pool = await create_pool(settings.database_url, max_size=5 + inline_n + 1)
        async with app.state.pool.acquire() as conn:
            await app.state.keys.reload(conn)
        app.state.waiter = queue.DoneWaiter(app.state.pool)
        await app.state.waiter.start()
        stop = asyncio.Event()
        key_reloader = asyncio.create_task(_reload_keys_forever(app, stop))
        inline: asyncio.Task[None] | None = None
        if settings.inline_analyzer:
            from tokensage.worker import run_worker

            inline = asyncio.create_task(
                run_worker(app.state.pool, settings, stop, concurrency=inline_n)
            )
            log.info("inline_analyzer.started")
        if not settings.api_key_pairs and not app.state.keys.has_keys:
            log.warning("auth.no_api_keys_configured")
        try:
            yield
        finally:
            stop.set()
            await key_reloader
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
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        resp.headers["X-Request-Id"] = rid
        if request.url.path.startswith("/v1"):
            key = getattr(request.state, "api_key", None)
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=resp.status_code,
                ms=int((time.perf_counter() - t0) * 1000),
                key=key.name if key else None,
            )
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
            from tokensage.api import usage

            use = await usage.all_today(conn)
        return JSONResponse(
            jsonable_encoder(
                {
                    "status": "ok",
                    "queue": {"pending": pending, "running": running, "failed_24h": failed_24h},
                    "sources": [dict(r) for r in health],
                    "usage_today": use,
                    "inline_analyzer": settings.inline_analyzer,
                }
            ),  # timestamptz values (e.g. source_health.open_until) need encoding
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(v1)
    app.include_router(admin)

    # Test console: a single static page that drives /v1 from the browser. It carries no
    # secrets; the user pastes an API key, which stays in their browser's localStorage.
    console = Path(__file__).resolve().parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    @app.get("/console", include_in_schema=False)
    async def console_page() -> FileResponse:
        return FileResponse(console, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()
