"""Authenticated, project-scoped MCP write tools."""

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import mcp_types as types
from fastapi import HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from kebivo.auth import _peer_ip
from kebivo.errors import error_response
from kebivo.feedback.ingest import (
    InvalidIdempotencyKey,
    InvalidSubmission,
    NotAuthorized,
    RateLimited,
    StorageBusy,
    ingest,
)
from kebivo.feedback.service import IdempotencyConflict
from kebivo.models import ApiToken, Project
from kebivo.tokens import (
    CORRECTION_SCOPE,
    GAP_SCOPE,
    ProjectTokenPrincipal,
    _corrections_enabled,
    authenticate_bearer,
)

GAP = "report_knowledge_gap"
CORRECTION = "submit_knowledge_correction"
_FIELDS = {
    "question": 10000,
    "correction": 20000,
    "context": 4000,
    "previous_answer": 10000,
    "source_reference": 2000,
    "source_event_id": 256,
    "conversation_id": 256,
    "agent_id": 128,
    "idempotency_key": 255,
}
_GAP_FIELDS = (
    "question",
    "context",
    "previous_answer",
    "tags",
    "source_event_id",
    "conversation_id",
    "agent_id",
    "idempotency_key",
)
_CORRECTION_FIELDS = (
    "question",
    "correction",
    "previous_answer",
    "source_reference",
    "tags",
    "source_event_id",
    "conversation_id",
    "agent_id",
    "idempotency_key",
)
_OUTPUT = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "status": {"type": "string", "const": "accepted"},
        "verification_status": {"type": "string", "const": "unverified"},
        "replayed": {"type": "boolean"},
    },
    "required": ["event_id", "status"],
    "additionalProperties": False,
}


def _schema(fields: tuple[str, ...], required: list[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in fields:
        if field == "tags":
            properties[field] = {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
            }
        else:
            properties[field] = {"type": "string", "maxLength": _FIELDS[field]}
            if field in {"question", "correction", "idempotency_key"}:
                properties[field]["minLength"] = 1
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
TOOLS = {
    GAP: types.Tool(
        name=GAP,
        description=(
            "Report a question that cannot be answered reliably from the information available "
            "to this assistant. This tool records a knowledge gap for follow-up. Use it when "
            "relevant information is missing or insufficient. Do not invent an answer to avoid "
            "reporting a gap. Omit idempotency_key unless retrying the identical report."
        ),
        input_schema=_schema(_GAP_FIELDS, ["question"]),
        output_schema=_OUTPUT,
        annotations=_ANNOTATIONS,
    ),
    CORRECTION: types.Tool(
        name=CORRECTION,
        description=(
            "Submit a user-provided correction or proposed answer for human review. Only use "
            "this tool when the user explicitly provides or authorizes the proposed correction. "
            "The submission is unverified and must not be treated as authoritative knowledge. "
            "Omit idempotency_key unless retrying the identical report."
        ),
        input_schema=_schema(_CORRECTION_FIELDS, ["question", "correction"]),
        output_schema=_OUTPUT,
        annotations=_ANNOTATIONS,
    ),
}


def _result(content: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        structured_content=content,
        content=[types.TextContent(text=json.dumps(content, separators=(",", ":")))],
        is_error=error,
    )


def _permissions(app: Any, principal: ProjectTokenPrincipal) -> Callable[[], set[str]]:
    def read() -> set[str]:
        with app.state.session_factory() as db:
            token = db.get(ApiToken, principal.token_id)
            project = db.get(Project, principal.project_id)
            if (
                token is None
                or project is None
                or token.revoked_at is not None
                or not project.is_active
                or project.archived_at is not None
            ):
                return set()
            try:
                scopes = set(json.loads(token.scopes_json))
            except (TypeError, ValueError):
                return set()
            visible = {GAP} if GAP_SCOPE in scopes else set()
            if CORRECTION_SCOPE in scopes and _corrections_enabled(project):
                visible.add(CORRECTION)
            return visible

    return read


def install_mcp_route(app: Any) -> Server[Any] | None:
    if not app.state.settings.mcp_enabled:
        return None

    async def visible(principal: ProjectTokenPrincipal) -> set[str]:
        return await run_in_threadpool(_permissions(app, principal))

    async def on_list_tools(ctx: Any, _params: Any) -> types.ListToolsResult:
        try:
            principal = ctx.request.state.mcp_principal
            names = await visible(principal)
            return types.ListToolsResult(
                tools=[TOOLS[name] for name in (GAP, CORRECTION) if name in names]
            )
        except Exception as exc:
            logging.getLogger("kebivo.mcp").error("mcp_tool_failed: %s", type(exc).__name__)
            raise MCPError(
                code=-32603, message=f"internal_error request_id={uuid.uuid4()}"
            ) from None

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = ctx.request.state.mcp_principal
        name = params.name
        try:
            names = await visible(principal)
        except Exception as exc:
            logging.getLogger("kebivo.mcp").error("mcp_tool_failed: %s", type(exc).__name__)
            return _result(
                {"error": {"code": "internal_error", "request_id": str(uuid.uuid4())}}, error=True
            )
        if name not in names:
            raise MCPError(code=-32602, message=f"Unknown tool: {name}")
        arguments = params.arguments or {}
        permitted = _GAP_FIELDS if name == GAP else _CORRECTION_FIELDS
        extra = sorted(set(arguments) - set(permitted))
        if extra:
            return _result(
                {
                    "error": {
                        "code": "validation_error",
                        "fields": [
                            {"field": field, "code": "extra_forbidden", "message": "Unknown field"}
                            for field in extra
                        ],
                    }
                },
                error=True,
            )
        payload = {
            **arguments,
            "event_type": "knowledge_gap" if name == GAP else "knowledge_correction",
        }
        key = payload.pop("idempotency_key", None)
        if key is not None and not isinstance(key, str):
            return _result(
                {
                    "error": {
                        "code": "validation_error",
                        "fields": [
                            {
                                "field": "idempotency_key",
                                "code": "invalid",
                                "message": "Invalid value",
                            }
                        ],
                    }
                },
                error=True,
            )
        try:
            result = await ingest(
                app.state.session_factory,
                app.state.settings,
                app.state.feedback_limiter,
                principal,
                payload,
                key,
                submitted_via="mcp",
                client_ip=ctx.request.client.host if ctx.request.client else "unknown",
            )
        except InvalidSubmission as exc:
            fields = [
                {
                    "field": str(item["loc"][0]) if item.get("loc") else None,
                    "code": str(item.get("type", "invalid")),
                    "message": "Invalid value",
                }
                for item in exc.fields
            ]
            return _result({"error": {"code": "validation_error", "fields": fields}}, error=True)
        except InvalidIdempotencyKey:
            return _result(
                {
                    "error": {
                        "code": "validation_error",
                        "fields": [
                            {
                                "field": "idempotency_key",
                                "code": "invalid",
                                "message": "Invalid value",
                            }
                        ],
                    }
                },
                error=True,
            )
        except NotAuthorized:
            raise MCPError(code=-32602, message=f"Unknown tool: {name}") from None
        except IdempotencyConflict:
            return _result({"error": {"code": "idempotency_conflict"}}, error=True)
        except RateLimited as exc:
            return _result(
                {"error": {"code": "rate_limited", "retry_after_seconds": exc.retry_after}},
                error=True,
            )
        except StorageBusy:
            return _result(
                {"error": {"code": "storage_busy", "retry_after_seconds": 1}}, error=True
            )
        except Exception as exc:
            logging.getLogger("kebivo.mcp").error("mcp_tool_failed: %s", type(exc).__name__)
            return _result(
                {"error": {"code": "internal_error", "request_id": str(uuid.uuid4())}}, error=True
            )
        if app.state.worker is not None:
            app.state.worker.wake()
        content: dict[str, Any] = {"event_id": result.event_id, "status": "accepted"}
        if name == CORRECTION:
            content["verification_status"] = "unverified"
        if result.replayed:
            content["replayed"] = True
        return _result(content)

    server: Server[Any] = Server(
        app.state.settings.display_name, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"]
    allowed_hosts += [urlsplit(origin).netloc for origin in app.state.settings.allowed_origins]
    allowed_hosts += list(app.state.settings.mcp_allowed_hosts)
    sdk_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=131_072,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(dict.fromkeys(allowed_hosts)),
            allowed_origins=list(app.state.settings.allowed_origins),
        ),
    )

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            app.state.feedback_limiter.check(
                "ip", _peer_ip(request), app.state.settings.feedback_rate_limit_per_ip
            )
        except HTTPException as exc:
            response = error_response(
                429,
                "rate_limited",
                "Feedback rate limit exceeded",
                scope.get("state", {}).get("request_id"),
            )
            response.headers["Retry-After"] = (exc.headers or {}).get("Retry-After", "1")
            await response(scope, receive, send)
            return
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        principal = None
        if separator and scheme.casefold() == "bearer" and token and token.strip() == token:

            def authenticate() -> ProjectTokenPrincipal:
                with app.state.session_factory() as db:
                    return authenticate_bearer(db, app.state.settings, token)[0]

            try:
                principal = await run_in_threadpool(authenticate)
            except HTTPException:
                pass
        if principal is None:
            response = error_response(
                401,
                "unauthorized",
                "Authentication required",
                scope.get("state", {}).get("request_id"),
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            await response(scope, receive, send)
            return
        if scope["method"] != "POST":
            response = error_response(
                405,
                "method_not_allowed",
                "Method not allowed",
                scope.get("state", {}).get("request_id"),
            )
            response.headers["Allow"] = "POST"
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["mcp_principal"] = principal
        await sdk_app(scope, receive, send)

    class ExactEndpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await endpoint(scope, receive, send)

    app.router.routes.append(
        Route("/mcp", endpoint=ExactEndpoint(), methods=["POST", "GET", "DELETE"])
    )
    return server
