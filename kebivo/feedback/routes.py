"""REST adapter for the protocol-neutral feedback service."""

import json
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kebivo.auth import _peer_ip
from kebivo.feedback.ingest import (
    InvalidIdempotencyKey,
    InvalidSubmission,
    NotAuthorized,
    RateLimited,
    ingest,
)
from kebivo.feedback.schemas import CorrectionSubmission, GapSubmission
from kebivo.feedback.service import IdempotencyConflict, submit_feedback
from kebivo.tokens import authenticate_project_token

GAP_EXAMPLE: dict[str, Any] = {
    "event_type": "knowledge_gap",
    "question": "How do I reset my corporate VPN?",
    "tags": ["it", "internal"],
    "agent_id": "employee-assistant",
    "conversation_id": "conv_123",
    "source_event_id": "msg_456",
    "context": "The bot could not find an approved procedure.",
    "metadata": {"department": "IT"},
}
CORRECTION_EXAMPLE: dict[str, Any] = {
    "event_type": "knowledge_correction",
    "question": "How long is the product warranty?",
    "correction": "The warranty period is 36 months for this product.",
    "previous_answer": "The warranty lasts 24 months.",
    "tags": ["customer-service", "warranty"],
    "source_reference": "Product warranty policy",
    "agent_id": "internal-support",
}
ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {"type": "object"},
                "request_id": {"type": ["string", "null"]},
            },
        }
    },
}


def install_feedback_routes(app: FastAPI) -> None:
    router = APIRouter(tags=["feedback"])

    @router.post(
        "/api/v1/feedback",
        status_code=202,
        summary="Durably accept a knowledge gap or correction",
        openapi_extra={
            "security": [{"ProjectBearer": []}],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                GapSubmission.model_json_schema(),
                                CorrectionSubmission.model_json_schema(),
                            ]
                        },
                        "examples": {
                            "gap": {"value": GAP_EXAMPLE},
                            "correction": {"value": CORRECTION_EXAMPLE},
                        },
                    }
                },
            },
        },
        responses={
            202: {
                "description": "Stored with a pending processing job",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "event_id": {"type": "string", "format": "uuid"},
                                "status": {"type": "string", "const": "accepted"},
                                "processing_status": {"type": "string"},
                            },
                        }
                    }
                },
            },
            **{
                status: {
                    "description": label,
                    "content": {"application/json": {"schema": ERROR_SCHEMA}},
                }
                for status, label in {
                    400: "Malformed JSON or invalid idempotency key",
                    401: "Invalid credentials",
                    403: "Missing scope or disabled corrections",
                    409: "Idempotency conflict",
                    413: "Body too large",
                    415: "Unsupported media type",
                    422: "Invalid fields",
                    429: "Rate limited",
                }.items()
            },
        },
    )
    async def post_feedback(request: Request) -> JSONResponse:
        principal, _project = await run_in_threadpool(authenticate_project_token, request)
        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(status_code=400, detail="Malformed JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        event_type = payload.get("event_type")
        if not isinstance(event_type, str) or event_type not in {
            "knowledge_gap",
            "knowledge_correction",
        }:
            raise RequestValidationError([{"loc": ("body", "event_type"), "type": "literal_error"}])
        try:
            result = await ingest(
                request.app.state.session_factory,
                request.app.state.settings,
                request.app.state.feedback_limiter,
                principal,
                payload,
                request.headers.get("idempotency-key"),
                submitted_via="rest",
                client_ip=_peer_ip(request),
                _submit=submit_feedback,
            )
        except NotAuthorized as exc:
            detail = {
                "scope_required": "Project token scope required",
                "corrections_disabled": "Project corrections are disabled",
                "unauthorized": "Invalid project token",
            }[exc.code]
            raise HTTPException(
                status_code=401 if exc.code == "unauthorized" else 403, detail=detail
            ) from None
        except InvalidSubmission as exc:
            raise RequestValidationError(exc.fields) from None
        except InvalidIdempotencyKey:
            raise HTTPException(status_code=400, detail="Invalid idempotency key") from None
        except RateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail="Feedback rate limit exceeded",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        except IdempotencyConflict:
            raise HTTPException(status_code=409, detail="Idempotency conflict") from None
        if request.app.state.worker is not None:
            request.app.state.worker.wake()
        headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
        return JSONResponse(
            status_code=202,
            headers=headers,
            content={
                "event_id": result.event_id,
                "status": "accepted",
                "processing_status": result.processing_status,
            },
        )

    app.include_router(router)
    # FastAPI's HTTP bearer definition for clients consuming OpenAPI.
    original_openapi = app.openapi

    def openapi() -> dict[str, object]:
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["ProjectBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "Project token",
        }
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
