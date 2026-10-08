"""One error shape for every failure: {"error": {"code", "message", "request_id"}}."""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    def __init__(
        self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
    ):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def invalid_ca(msg: str = "not a valid Solana address") -> ApiError:
    return ApiError(400, "invalid_ca", msg)


def unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "missing or unknown API key")


def forbidden() -> ApiError:
    return ApiError(403, "forbidden", "admin key required")


def not_found(code: str = "not_found", msg: str = "not found") -> ApiError:
    return ApiError(404, code, msg)


def rate_limited(retry_after_s: int, msg: str = "rate limit exceeded") -> ApiError:
    return ApiError(429, "rate_limited", msg, headers={"Retry-After": str(retry_after_s)})


def quota_exceeded(kind: str, retry_after_s: int) -> ApiError:
    return ApiError(
        429,
        "quota_exceeded",
        f"daily quota for {kind} calls exhausted for this key",
        headers={
            "Retry-After": str(retry_after_s),
            **({"X-Quota-Full-Remaining": "0"} if kind == "full-depth" else {}),
            **({"X-Quota-Refresh-Remaining": "0"} if kind == "refresh" else {}),
        },
    )


def overloaded(retry_after_s: int = 10) -> ApiError:
    return ApiError(
        503,
        "overloaded",
        "job queue is full; retry later",
        headers={"Retry-After": str(retry_after_s)},
    )


def validation(msg: str) -> ApiError:
    return ApiError(422, "validation_error", msg)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    rid = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.detail, "request_id": rid}},
        headers=exc.headers,
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    rid = getattr(request.state, "request_id", "")
    code = {404: "not_found", 405: "method_not_allowed", 422: "validation_error"}.get(
        exc.status_code, "error"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail), "request_id": rid}},
        headers=exc.headers,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything unexpected still answers in the one error shape (a bare 500 otherwise)."""
    rid = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal", "message": "internal error", "request_id": rid}},
    )
