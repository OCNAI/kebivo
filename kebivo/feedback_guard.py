"""ASGI request body limits enforced before endpoint body consumption."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kebivo.errors import error_response

MAX_FEEDBACK_BODY = 131_072
MAX_AUTH_BODY = 65_536
MAX_API_BODY = 1_048_576
FEEDBACK_PATHS = {"/api/v1/feedback", "/mcp"}
WRITE_METHODS = {"POST", "PUT", "PATCH"}


def body_limit(path: str, method: str) -> int | None:
    """Return the byte limit for a request, or None when the body is not guarded."""
    if path in FEEDBACK_PATHS:
        return MAX_FEEDBACK_BODY if method == "POST" else None
    if method not in WRITE_METHODS or not path.startswith("/api/v1/"):
        return None
    if path.startswith("/api/v1/auth/"):
        return MAX_AUTH_BODY
    return MAX_API_BODY


class FeedbackBodyGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = body_limit(path, scope.get("method", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        request_id = scope.get("state", {}).get("request_id")
        message_text = (
            "Feedback body is too large" if path in FEEDBACK_PATHS else "Request body is too large"
        )
        raw_length = headers.get(b"content-length", b"")
        try:
            length = int(raw_length) if raw_length else None
        except ValueError:
            length = None
        if length is not None and length > limit:
            await error_response(413, "payload_too_large", message_text, request_id)(
                scope, receive, send
            )
            return
        # Media checks apply only to REST feedback; other routes keep FastAPI's handling.
        if path != "/api/v1/feedback":
            content_type = None
        else:
            content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
        parts = [part.strip() for part in content_type.split(";")] if content_type else []
        if content_type is not None and (
            parts[0] != "application/json" or any(part != "charset=utf-8" for part in parts[1:])
        ):
            await error_response(
                415, "unsupported_media_type", "JSON content type required", request_id
            )(scope, receive, send)
            return
        if content_type is not None and (
            headers.get(b"content-encoding", b"identity").decode("latin-1").lower().strip()
            != "identity"
        ):
            await error_response(
                415, "unsupported_media_type", "Unsupported content encoding", request_id
            )(scope, receive, send)
            return
        total = 0
        buffered: list[Message] = []
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > limit:
                await error_response(413, "payload_too_large", message_text, request_id)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)
