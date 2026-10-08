"""The /v1 routes (guide §6.4)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from tokensage import __version__, queue
from tokensage.api import errors, service
from tokensage.api.auth import ApiKey, require_api_key
from tokensage.api.schemas import (
    SCHEMA_VERSION,
    BatchItem,
    BatchRequest,
    BatchRequestItem,
    BatchResponse,
    Depth,
    FlagEntry,
    JobResponse,
    MetaResponse,
    RawFields,
    TaxonomyEntry,
    TokenHints,
    TokenRequest,
    TokenResponse,
    Versions,
    stored_analysis,
)
from tokensage.resolve.pump_ca import parse_ca
from tokensage.taxonomy import load_taxonomy
from tokensage.versions import LEXICON_VERSION, RULES_VERSION

router = APIRouter(prefix="/v1", tags=["v1"])

DISCLAIMER = (
    "TokenSage explains what a token appears to reference. Categories, flags and "
    "confidences are informational only and are not financial advice. Image URLs are "
    "returned unscreened; the consumer decides what to display."
)


def _ca(raw: str) -> str:
    try:
        return parse_ca(raw)
    except ValueError as e:
        raise errors.invalid_ca(str(e)) from None


INCLUDE_DOC = (
    "comma list of optional parts to keep: evidence, raw. Omit to get everything; "
    "a part left out of the list is dropped (evidence -> [], raw -> empty)"
)
TOKEN_RESPONSES: dict[int | str, dict] = {
    202: {"model": TokenResponse, "description": "Analysis pending; poll again."},
    400: {"description": "invalid_ca"},
    401: {"description": "unauthorized"},
    404: {"description": "token_not_found (never when hints with metadata were given)"},
    422: {"description": "not_a_token_mint | not_pumpfun"},
    429: {"description": "rate_limited | quota_exceeded"},
    503: {"description": "overloaded"},
}


@router.get(
    "/tokens/{ca}",
    response_model=TokenResponse,
    responses=TOKEN_RESPONSES,
    summary="Analyze a pump.fun token by contract address",
)
async def get_token(
    request: Request,
    response: Response,
    ca: str,
    key: Annotated[ApiKey, Depends(require_api_key)],
    depth: Depth | None = None,
    wait: Annotated[int | None, Query(ge=0, le=25)] = None,
    max_age: Annotated[int | None, Query(ge=0, le=7 * 86400)] = None,
    refresh: bool = False,
    include: Annotated[str | None, Query(description=INCLUDE_DOC)] = None,
) -> TokenResponse:
    return await _token(
        request, response, ca, key, depth, wait, max_age, refresh, include, hints=None
    )


@router.post(
    "/tokens/{ca}",
    response_model=TokenResponse,
    responses=TOKEN_RESPONSES,
    summary="Analyze a token, passing metadata hints you already have",
    description=(
        "Same as GET /v1/tokens/{ca} (same query parameters and responses), with an optional "
        "JSON body carrying `hints`. With hints the metadata fetch is skipped, and a mint not "
        "yet visible on-chain is analysed from the hints instead of returning 404."
    ),
)
async def post_token(
    request: Request,
    response: Response,
    ca: str,
    key: Annotated[ApiKey, Depends(require_api_key)],
    body: TokenRequest | None = None,
    depth: Depth | None = None,
    wait: Annotated[int | None, Query(ge=0, le=25)] = None,
    max_age: Annotated[int | None, Query(ge=0, le=7 * 86400)] = None,
    refresh: bool = False,
    include: Annotated[str | None, Query(description=INCLUDE_DOC)] = None,
) -> TokenResponse:
    hints = body.hints if body else None
    return await _token(
        request, response, ca, key, depth, wait, max_age, refresh, include, hints=hints
    )


def _hints_dict(h: TokenHints | None) -> dict | None:
    if h is None:
        return None
    d = {k: v for k, v in h.model_dump(mode="json").items() if v not in (None, "")}
    return d or None


async def _token(
    request: Request,
    response: Response,
    ca: str,
    key: ApiKey,
    depth: Depth | None,
    wait: int | None,
    max_age: int | None,
    refresh: bool,
    include: str | None,
    hints: TokenHints | None,
) -> TokenResponse:
    settings = request.app.state.settings
    mint = _ca(ca)
    depth_v: Depth = depth or settings.default_depth
    wait_v = settings.default_wait_s if wait is None else min(wait, settings.max_wait_s)
    res = await service.get_or_enqueue(
        request.app.state.pool,
        request.app.state.waiter,
        settings,
        mint=mint,
        depth=depth_v,
        wait_s=wait_v,
        max_age_s=max_age,
        refresh=refresh,
        requested_by=key.name,
        request_id=request.state.request_id,
        key=key,
        hints=_hints_dict(hints),
    )
    if res.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = "3"
    # a response served from the cache created no job, so charged nothing: the counters the
    # auth step just read are current
    cache_hit = res.analysis is not None and res.freshness.from_cache
    known = getattr(request.state, "usage", None) if cache_hit else None
    response.headers.update(await service.quota_headers(request.app.state.pool, key, known))
    _apply_include(res, include)
    return res


def _apply_include(res: TokenResponse, include: str | None) -> None:
    if include is None:
        return
    parts = {p.strip().lower() for p in include.split(",") if p.strip()}
    for a in (res.analysis, res.stale_analysis):
        if a is None:
            continue
        if "evidence" not in parts:
            a.evidence = []
        if "raw" not in parts:
            a.raw = RawFields()


@router.post("/tokens:batch", response_model=BatchResponse, summary="Prefetch up to 50 CAs")
async def batch(
    request: Request,
    response: Response,
    body: BatchRequest,
    key: Annotated[ApiKey, Depends(require_api_key)],
) -> BatchResponse:
    settings = request.app.state.settings
    entries = [BatchRequestItem(ca=c) for c in body.cas] + list(body.items)
    if not entries:
        raise errors.validation("give at least one CA in `cas` or `items`")
    if len(entries) > settings.batch_max:
        raise errors.validation(f"at most {settings.batch_max} CAs per batch")
    cb: str | None = None
    if body.callback_url:
        from tokensage import callbacks
        from tokensage.net.safe_fetch import FetchError, UnsafeUrl

        try:
            cb = await callbacks.validate_callback_url(body.callback_url)
        except (UnsafeUrl, FetchError) as e:
            raise errors.ApiError(400, "invalid_callback_url", str(e)) from None
    items: list[BatchItem] = []
    for entry in entries:
        raw = entry.ca
        try:
            mint = parse_ca(raw)
        except ValueError as e:
            items.append(BatchItem(ca=raw, status="invalid", error=str(e)))
            continue
        try:
            res = await service.get_or_enqueue(
                request.app.state.pool,
                request.app.state.waiter,
                settings,
                mint=mint,
                depth=body.depth,
                wait_s=0,
                max_age_s=None,
                refresh=False,
                requested_by=key.name,
                request_id=request.state.request_id,
                priority=queue.PRIORITY_BATCH,
                key=key,
                callback_url=cb,
                hints=_hints_dict(entry.hints),
            )
        except errors.ApiError as e:
            # Fail this item only (quota/overload rejection, or a recent definitive
            # failure such as token_not_found); the other items are still answered.
            retry = (e.headers or {}).get("Retry-After")
            items.append(
                BatchItem(
                    ca=mint,
                    status="failed",
                    error=e.code,
                    retry_after_s=int(retry) if retry and retry.isdigit() else None,
                )
            )
            continue
        items.append(
            BatchItem(
                ca=mint,
                status=res.status,
                analysis=res.analysis,
                job_id=res.job_id,
                # a failed analysis: say why, as the single-CA response does in `errors`
                error=res.errors[0].detail if res.status == "failed" and res.errors else None,
            )
        )
    response.headers.update(await service.quota_headers(request.app.state.pool, key))
    return BatchResponse(items=items, request_id=request.state.request_id)


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Poll an analysis job")
async def get_job(
    request: Request,
    job_id: int,
    key: Annotated[ApiKey, Depends(require_api_key)],
) -> JobResponse:
    pool = request.app.state.pool
    rid = request.state.request_id
    async with pool.acquire() as conn:
        job = await queue.get(conn, job_id)
        if job is None:
            raise errors.not_found("job_not_found", "no such job")
        result: TokenResponse | None = None
        if job.status == "done" and job.mint and job.depth:
            latest = await service.latest_analysis(conn, job.mint, job.depth)
            if latest:
                doc, _ = latest
                result = TokenResponse(
                    ca=job.mint,
                    status=service._status_for(doc),  # type: ignore[arg-type]
                    depth=doc["depth"],
                    analysis=stored_analysis(doc),
                    request_id=rid,
                )
    return JobResponse(
        job_id=job.id,
        status=job.status,  # type: ignore[arg-type]
        ca=job.mint,
        depth=job.depth,  # type: ignore[arg-type]
        result=result,
        error=(f"{job.error_code}: " if job.error_code else "") + (job.last_error or "")
        if job.status == "failed"
        else None,
        request_id=rid,
    )


@router.get("/meta", response_model=MetaResponse, summary="Versions, taxonomy and flag codes")
async def meta(key: Annotated[ApiKey, Depends(require_api_key)]) -> MetaResponse:
    tax = load_taxonomy()
    depths: list[Literal["basic", "full"]] = ["basic", "full"]
    return MetaResponse(
        schema_version=SCHEMA_VERSION,
        service_version=__version__,
        versions=Versions(rules=RULES_VERSION, lexicon=LEXICON_VERSION),
        categories=[TaxonomyEntry(**c) for c in tax["categories"]],
        flags=[FlagEntry(**f) for f in tax["flags"]],
        depths=depths,
        disclaimer=DISCLAIMER,
    )
