"""The /v1 routes (guide §6.4)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from tokensage import __version__, queue
from tokensage.analyzer import LEXICON_VERSION, RULES_VERSION
from tokensage.api import errors, service
from tokensage.api.auth import ApiKey, require_api_key
from tokensage.api.schemas import (
    SCHEMA_VERSION,
    Analysis,
    BatchItem,
    BatchRequest,
    BatchResponse,
    Depth,
    FlagEntry,
    JobResponse,
    MetaResponse,
    TaxonomyEntry,
    TokenResponse,
    Versions,
)
from tokensage.resolve.pump_ca import parse_ca
from tokensage.taxonomy import load_taxonomy

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


@router.get(
    "/tokens/{ca}",
    response_model=TokenResponse,
    responses={
        202: {"model": TokenResponse, "description": "Analysis pending; poll again."},
        400: {"description": "invalid_ca"},
        401: {"description": "unauthorized"},
        404: {"description": "token_not_found"},
        422: {"description": "not_a_token_mint | not_pumpfun"},
        429: {"description": "rate_limited"},
        503: {"description": "overloaded"},
    },
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
    include: Annotated[str | None, Query(description="comma list: evidence,raw")] = None,
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
    )
    if res.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = "3"
    if include is not None and "evidence" not in include.split(",") and res.analysis:
        res.analysis.evidence = []
    return res


@router.post("/tokens:batch", response_model=BatchResponse, summary="Prefetch up to 50 CAs")
async def batch(
    request: Request,
    body: BatchRequest,
    key: Annotated[ApiKey, Depends(require_api_key)],
) -> BatchResponse:
    settings = request.app.state.settings
    if len(body.cas) > settings.batch_max:
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
    for raw in body.cas:
        try:
            mint = parse_ca(raw)
        except ValueError as e:
            items.append(BatchItem(ca=raw, status="invalid", error=str(e)))
            continue
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
        )
        items.append(
            BatchItem(ca=mint, status=res.status, analysis=res.analysis, job_id=res.job_id)
        )
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
                    status="complete",
                    depth=doc["depth"],
                    analysis=Analysis.model_validate(doc),
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
