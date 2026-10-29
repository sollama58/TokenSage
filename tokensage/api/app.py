"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tokensage import __version__, queue
from tokensage.api import errors
from tokensage.api.auth import KeyStore, require_admin
from tokensage.api.routes_admin import read_router as admin_read
from tokensage.api.routes_admin import router as admin
from tokensage.api.routes_monitor import router as monitor
from tokensage.api.routes_v1 import router as v1
from tokensage.config import Settings, get_settings
from tokensage.db import create_pool
from tokensage.logging import configure_logging
from tokensage.net import metrics

# An exact Swagger UI release for /docs, never a floating major.
SWAGGER_UI_VERSION = "5.17.14"

log = structlog.get_logger("api")

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
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


# The largest legitimate body is a 50-item batch with full hints (about 650 KB); bodies are
# parsed before the API key is checked, so anything bigger is refused unread (audit
# 2026-10-14 M8).
MAX_BODY_BYTES = 1024 * 1024


class BodyLimit:
    """413 for a request body over max_bytes: by Content-Length up front, and by counting
    the bytes of a chunked body as they arrive."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_bytes
            except ValueError:
                too_big = False
            if too_big:
                await _too_large(scope, receive, send)
                return
        seen = 0
        started = refused = False

        async def counted() -> Message:
            # past the cap: answer 413 now and tell the app the client went away (an
            # exception raised here would reach it as a body parse error, a 400)
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    refused = True
                    if not started:
                        await _too_large(scope, receive, send)
                    return {"type": "http.disconnect"}
            return msg

        async def tracked(msg: Message) -> None:
            nonlocal started
            if refused:
                return  # the 413 went out instead
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        await self.app(scope, counted, tracked)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    resp = JSONResponse(
        status_code=413,
        content={"error": {"code": "payload_too_large", "message": "request body too large"}},
    )
    await resp(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.keys = KeyStore(settings)
        metrics.meter.configure(
            rpc_url=settings.solana_rpc_url,
            ipfs_gateways=settings.ipfs_gateway_list,
            costs=settings.helius_credit_costs,
        )
        inline_n = max(1, settings.inline_worker_concurrency) if settings.inline_analyzer else 0
        # API requests share the pool with the inline analyzer's job connections
        app.state.pool = await create_pool(settings.database_url, max_size=5 + inline_n + 1)
        async with app.state.pool.acquire() as conn:
            await app.state.keys.reload(conn)
        app.state.waiter = queue.DoneWaiter(app.state.pool)
        await app.state.waiter.start()
        # fixed-host reads the API makes itself (the pump.fun relay, routes_v1.py), metered
        # like the analyzer's and never through an env proxy
        app.state.http = httpx.AsyncClient(
            headers={"User-Agent": settings.http_user_agent},
            trust_env=False,
            follow_redirects=False,
            transport=metrics.MeteredTransport(),
        )
        stop = asyncio.Event()
        key_reloader = asyncio.create_task(_reload_keys_forever(app, stop))
        # outbound call counts (the inline analyzer's) and breaker state for the admin panel
        flusher = asyncio.create_task(metrics.run_flusher(app.state.pool, stop))
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
            await flusher
            await app.state.http.aclose()
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
        # Served below with a pinned Swagger UI: FastAPI's default page loads swagger-ui-dist@5,
        # a floating version, on the origin whose console keeps the keys (security audit
        # 2026-10-16, L1).
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/docs", include_in_schema=False)
    async def docs_page() -> Response:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title="TokenSage - API docs",
            swagger_js_url=f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js",
            swagger_css_url=f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css",
        )

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        # A caller's id is echoed and logged on every line, so only a short, plain one is kept.
        sent = request.headers.get("x-request-id") or ""
        rid = sent if _REQUEST_ID_RE.fullmatch(sent) else uuid.uuid4().hex[:16]
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

    app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)

    app.add_exception_handler(errors.ApiError, errors.api_error_handler)  # type: ignore[arg-type]
    # Starlette's router raises its own HTTPException (the parent of FastAPI's) for an
    # unknown route or method; registering the parent covers both, so a 404/405 answers in
    # the one error shape instead of FastAPI's {"detail": ...}
    app.add_exception_handler(StarletteHTTPException, errors.http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, errors.unhandled_error_handler)

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
    app.include_router(admin_read)
    app.include_router(admin)
    app.include_router(monitor)

    # Test console: a single static page that drives /v1 from the browser. It carries no
    # secrets; the user pastes an API key, which stays in their browser's localStorage.
    console = Path(__file__).resolve().parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    @app.get("/console", include_in_schema=False)
    async def console_page() -> FileResponse:
        return FileResponse(console, media_type="text/html", headers={"Cache-Control": "no-cache"})

    # Admin panel: a static page over /admin/v1. Like the console it carries no secrets and
    # no data; it asks for the ADMIN_KEY and every number it shows needs that key.
    admin_page = Path(__file__).resolve().parent / "static" / "admin.html"

    @app.get("/admin", include_in_schema=False)
    async def admin_panel() -> FileResponse:
        return FileResponse(
            admin_page,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()
