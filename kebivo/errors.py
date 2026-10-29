"""Stable API error responses without reflecting submitted values or tracebacks."""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("kebivo.errors")


def error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": request_id,
            }
        },
        headers=headers,
    )


def _code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
    }.get(status, f"http_{status}")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if not request.url.path.startswith("/api/v1"):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if request.url.path == "/api/v1/feedback" and exc.status_code == 401:
            from kebivo.feedback_limits import record_feedback_auth_failure

            try:
                record_feedback_auth_failure(request)
            except HTTPException as limit_error:
                exc = limit_error
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        headers = dict(exc.headers) if exc.headers else None
        code = {
            "Project token scope required": "scope_required",
            "Project corrections are disabled": "corrections_disabled",
            "Invalid idempotency key": "invalid_idempotency_key",
            "Idempotency conflict": "idempotency_conflict",
            "Feedback storage busy; retry with the same idempotency key": "storage_busy",
            "Feedback storage busy; retry": "storage_busy",
        }.get(message, _code(exc.status_code))
        return error_response(
            exc.status_code,
            code,
            message,
            getattr(request.state, "request_id", None),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for item in exc.errors():
            location = [
                str(part)
                for part in item.get("loc", ())
                if part not in {"body", "query", "path", "header"}
            ]
            fields.append(
                {
                    "field": ".".join(location),
                    "code": str(item.get("type", "invalid")),
                    "message": "Invalid value",
                }
            )
        return error_response(
            422,
            "validation_error",
            "Invalid request",
            getattr(request.state, "request_id", None),
            {"fields": fields},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception class: %s", type(exc).__name__)
        return error_response(
            500,
            "internal_error",
            "Internal server error",
            getattr(request.state, "request_id", None),
        )
