"""Feedback boundary, envelope, rate limit, and verifier regression tests."""

import logging
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.requests import Request as StarletteRequest

from kebivo.app import create_app
from kebivo.feedback_limits import check_feedback_rate_limit, record_feedback_auth_failure
from kebivo.models import ApiToken, now_utc
from kebivo.settings import Settings
from kebivo.tokens import authenticate_project_token, authorize_feedback, touch_project_token


def app_with_feedback(tmp_path: Path, *, ip_limit: int = 300, token_limit: int = 120) -> FastAPI:
    settings = Settings(
        "test",
        "test",
        None,
        str(tmp_path / "guard.db"),
        feedback_rate_limit_per_ip=ip_limit,
        feedback_rate_limit_per_token=token_limit,
    )
    app = create_app(settings)

    class Body(BaseModel):
        question: str = Field(min_length=1)

    @app.post("/api/v1/feedback")
    async def feedback(request: Request) -> dict[str, int]:
        principal, project = authenticate_project_token(request)
        payload = await request.json()
        authorize_feedback(principal, project, payload.get("event_type", "knowledge_gap"))
        check_feedback_rate_limit(request, principal.token_id)
        return {"size": len(await request.body())}

    @app.post("/api/v1/test-validation")
    def validate(body: Body) -> Body:
        return body

    @app.get("/api/v1/test-error")
    def fail() -> None:
        raise RuntimeError("private trace")

    # These guard-specific routes must shadow the production feedback route.
    # The SPA catchall exists only when the frontend bundle has been built.
    # Newer FastAPI includes routers as nested entries. Insert before the
    # first included router so this test handler shadows production feedback.
    shadow_route = next(
        (
            i
            for i, route in enumerate(app.router.routes)
            if getattr(route, "path", None) is None
        ),
        len(app.router.routes) - 3,
    )
    new_routes = app.router.routes[-3:]
    del app.router.routes[-3:]
    app.router.routes[shadow_route:shadow_route] = new_routes
    return app


def test_envelope_validation_and_500_have_request_id_without_values(tmp_path: Path) -> None:
    app = app_with_feedback(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        bad = client.post(
            "/api/v1/test-validation",
            json={"question": ""},
            headers={"X-Request-ID": "validation-1"},
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["request_id"] == "validation-1"
        assert bad.json()["error"]["details"]["fields"][0]["field"] == "question"
        assert "input" not in str(bad.json())
        failed = client.get("/api/v1/test-error", headers={"X-Request-ID": "failure-1"})
        assert failed.status_code == 500
        assert failed.json() == {
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
                "details": {},
                "request_id": "failure-1",
            }
        }
        assert "private trace" not in failed.text


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("size,status", [(131_072, 200), (131_073, 413)])
def test_feedback_body_boundary(tmp_path: Path, chunked: bool, size: int, status: int) -> None:
    app = app_with_feedback(tmp_path)
    with TestClient(app) as client:
        # An invalid token is checked after an accepted-size body reaches the route.
        body = b" " * size
        content: bytes | Iterator[bytes]
        if chunked:

            def chunks() -> Iterator[bytes]:
                yield body[:70_000]
                yield body[70_000:]

            content = chunks()
        else:
            content = body
        response = client.post(
            "/api/v1/feedback", content=content, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == (401 if status == 200 else 413)
        if status == 413:
            assert response.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize(
    "method,path,limit",
    [
        ("POST", "/api/v1/auth/login", 65_536),
        ("POST", "/api/v1/projects", 1_048_576),
        ("PATCH", "/api/v1/admin/groups/1", 1_048_576),
        ("PUT", "/api/v1/projects/1/routing-rules", 1_048_576),
    ],
)
def test_excessive_request_size_on_api_writes(
    tmp_path: Path, chunked: bool, method: str, path: str, limit: int
) -> None:
    """§25.4 Excessive request size: login and admin writes are capped before parsing."""
    app = app_with_feedback(tmp_path)
    with TestClient(app) as client:
        for size, rejected in ((limit, False), (limit + 1, True)):
            body = b" " * size
            content: bytes | Iterator[bytes]
            if chunked:

                def chunks(data: bytes = body) -> Iterator[bytes]:
                    yield data[: len(data) // 2]
                    yield data[len(data) // 2 :]

                content = chunks()
            else:
                content = body
            response = client.request(
                method,
                path,
                content=content,
                headers={"Content-Type": "application/json", "X-Request-ID": "size-1"},
            )
            if rejected:
                assert response.status_code == 413
                assert response.json()["error"] == {
                    "code": "payload_too_large",
                    "message": "Request body is too large",
                    "details": {},
                    "request_id": "size-1",
                }
            else:
                # An accepted-size body reaches the route (auth or validation rejects it).
                assert response.status_code != 413


def test_reads_and_non_api_paths_are_not_body_guarded(tmp_path: Path) -> None:
    from kebivo.feedback_guard import body_limit

    assert body_limit("/api/v1/projects", "GET") is None
    assert body_limit("/api/v1/projects/1", "DELETE") is None
    assert body_limit("/assets/app.js", "POST") is None
    assert body_limit("/api/v1/feedback", "POST") == 131_072
    assert body_limit("/mcp", "POST") == 131_072


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        {"Content-Type": "application/json; charset=latin-1"},
    ],
)
def test_feedback_media_guard(tmp_path: Path, headers: dict[str, str]) -> None:
    with TestClient(app_with_feedback(tmp_path)) as client:
        response = client.post("/api/v1/feedback", content=b"{}", headers=headers)
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_rate_limit_and_trusted_proxy_rule(tmp_path: Path) -> None:
    app = app_with_feedback(tmp_path, ip_limit=2, token_limit=1)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/feedback",
        "app": app,
        "headers": [(b"x-forwarded-for", b"198.51.100.7")],
        "client": ("203.0.113.5", 1234),
    }
    request = StarletteRequest(scope)
    record_feedback_auth_failure(request)
    record_feedback_auth_failure(request)
    with pytest.raises(HTTPException) as exc:
        record_feedback_auth_failure(request)
    assert exc.value.status_code == 429
    assert exc.value.headers is not None
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_token_last_use_is_staged_and_throttled() -> None:
    db = MagicMock()
    row = ApiToken(
        project_id="project",
        name="token",
        token_hash="hash",
        token_prefix="prefix",
        scopes_json="[]",
    )
    db.get.return_value = row
    first = now_utc()
    touch_project_token(db, "token", now=first)
    assert row.last_used_at == first
    touch_project_token(db, "token", now=first + timedelta(seconds=59))
    assert row.last_used_at == first
    later = first + timedelta(seconds=60)
    touch_project_token(db, "token", now=later)
    assert row.last_used_at == later
    db.commit.assert_not_called()


def test_request_logs_exclude_body_authorization_and_idempotency_key(tmp_path: Path) -> None:
    app = app_with_feedback(tmp_path)
    logger = logging.getLogger("kebivo.request")
    with TestClient(app) as client:
        handler = logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        stream = StringIO()
        original = handler.stream
        handler.setStream(stream)
        try:
            client.post(
                "/api/v1/feedback",
                content=b'{"question":"private question"}',
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer secret-token",
                    "Idempotency-Key": "secret-key",
                },
            )
        finally:
            handler.setStream(original)
    log = stream.getvalue()
    for secret in ("private question", "secret-token", "secret-key", "authorization"):
        assert secret not in log.lower()


def test_failed_auth_counts_toward_ip_limit_and_security_headers(tmp_path: Path) -> None:
    with TestClient(app_with_feedback(tmp_path, ip_limit=2)) as client:
        responses = [
            client.post(
                "/api/v1/feedback", content=b"{}", headers={"Content-Type": "application/json"}
            )
            for _ in range(3)
        ]
    assert [response.status_code for response in responses] == [401, 401, 429]
    assert int(responses[-1].headers["Retry-After"]) >= 1
    assert responses[-1].json()["error"]["request_id"] == responses[-1].headers["x-request-id"]
    assert responses[-1].headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in responses[-1].headers["content-security-policy"]
    assert responses[-1].headers["referrer-policy"] == "no-referrer"


def test_token_counter_limits_independently_of_ip(tmp_path: Path) -> None:
    app = app_with_feedback(tmp_path, ip_limit=10, token_limit=1)
    request = StarletteRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/feedback",
            "app": app,
            "headers": [],
            "client": ("203.0.113.5", 1234),
        }
    )
    check_feedback_rate_limit(request, "token-1")
    with pytest.raises(HTTPException) as exc:
        check_feedback_rate_limit(request, "token-1")
    assert exc.value.status_code == 429
    check_feedback_rate_limit(request, "token-2")
