"""Contracts for the standalone standard-library Python SDK."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import kebivo_client as sdk
import pytest
import uvicorn
from sqlalchemy import func, select

import kebivo
from kebivo.models import FeedbackEvent
from kebivo.tests.test_projects import make_client
from kebivo.tests.test_tokens import create_project, issue_token


@pytest.fixture
def stub() -> Any:
    received: list[tuple[dict[str, str], dict[str, Any]]] = []
    replies: list[tuple[int, dict[str, str], dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((dict(self.headers), body))
            status, headers, result = replies.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received, replies
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_retry_preserves_key_and_payload(stub: Any, monkeypatch: Any) -> None:
    url, received, replies = stub
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    replies.extend(
        [
            (
                429,
                {"Retry-After": "3"},
                {"error": {"code": "rate_limited", "message": "Slow down"}},
            ),
            (503, {}, {"error": {"code": "internal_error", "message": "Retry"}}),
            (202, {}, {"event_id": "event-1", "status": "accepted"}),
        ]
    )
    result = sdk.Kebivo(url, "token", timeout=0.5).submit_correction(
        "Question?", "Correct answer", tags=["docs"], source_reference="manual"
    )
    assert result["event_id"] == "event-1"
    assert waits == [3, 2]
    assert len({headers["Idempotency-Key"] for headers, _ in received}) == 1
    assert all(headers["Authorization"] == "Bearer token" for headers, _ in received)
    assert all(body == received[0][1] for _, body in received)
    assert received[0][1]["event_type"] == "knowledge_correction"
    assert received[0][1]["source_reference"] == "manual"


def test_http_error_is_typed_and_not_retried(stub: Any, monkeypatch: Any) -> None:
    url, received, replies = stub
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("unexpected retry"))
    replies.append(
        (
            422,
            {"X-Request-ID": "request-1"},
            {
                "error": {
                    "code": "validation_error",
                    "message": "Invalid request",
                    "details": {"field": "question"},
                }
            },
        )
    )
    with pytest.raises(sdk.KebivoHTTPError) as caught:
        sdk.Kebivo(url, "token").report_gap("", idempotency_key="fixed")
    assert caught.value.status_code == 422
    assert caught.value.code == "validation_error"
    assert caught.value.request_id == "request-1"
    assert caught.value.details == {"field": "question"}
    assert len(received) == 1
    assert received[0][0]["Idempotency-Key"] == "fixed"


def test_connection_retries_then_typed_error(monkeypatch: Any) -> None:
    keys: list[str] = []
    waits: list[float] = []

    def fail(self: Any, data: bytes, key: str) -> None:
        keys.append(key)
        raise OSError("offline")

    monkeypatch.setattr(sdk.Kebivo, "_request", fail)
    monkeypatch.setattr(time, "sleep", waits.append)
    with pytest.raises(sdk.KebivoConnectionError):
        sdk.Kebivo("https://example.test", "token", timeout=0.25).report_gap("Question?")
    assert len(keys) == 4 and len(set(keys)) == 1
    assert waits == [1, 2, 4]


def test_server_and_sdk_import_side_by_side() -> None:
    assert kebivo.__name__ == "kebivo"
    assert sdk.__name__ == "kebivo_client"
    assert kebivo is not sdk


def test_invalid_input_before_request(monkeypatch: Any) -> None:
    monkeypatch.setattr(sdk.Kebivo, "_request", lambda *_: pytest.fail("unexpected request"))
    for key in ("", "a b", "a\n", "é", "a" * 256):
        with pytest.raises(sdk.KebivoValidationError):
            sdk.Kebivo("https://example.test", "token").report_gap("Question?", idempotency_key=key)
    with pytest.raises(sdk.KebivoValidationError):
        sdk.Kebivo("https://user:secret@example.test", "token")


def test_insecure_warning_once(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setattr(sdk, "_warned_insecure", False)
    sdk.Kebivo("http://example.test", "token")
    sdk.Kebivo("http://other.test", "token")
    sdk.Kebivo("http://127.0.0.1", "token")
    assert caplog.text.count("token will be sent over HTTP") == 1


def test_slow_body_has_total_deadline() -> None:
    class Drip(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(10):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    break
                time.sleep(0.4)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = sdk.Kebivo(f"http://127.0.0.1:{server.server_port}", "token", timeout=1)
        started = time.monotonic()
        with pytest.raises(sdk.KebivoConnectionError):
            client.report_gap("Question?")
        assert time.monotonic() - started < 2
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_sdk_against_real_app(tmp_path: Path) -> None:
    admin, app = make_client(tmp_path / "sdk.db")
    project = create_project(admin, "sdk")
    token = issue_token(admin, project["id"], "sdk").json()["token"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.03)
        assert server.started
        client = sdk.Kebivo(f"http://127.0.0.1:{port}", token)
        first = client.report_gap(
            "Where is the runbook?", tags=["docs"], idempotency_key="sdk-real"
        )
        replay = client.report_gap(
            "Where is the runbook?", tags=["docs"], idempotency_key="sdk-real"
        )
        assert first["event_id"] == replay["event_id"]
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        admin.close()
        app.state.engine.dispose()


def test_redirect_is_not_followed(stub: Any, monkeypatch: Any) -> None:
    url, received, replies = stub
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("unexpected retry"))
    replies.append((302, {"Location": "http://127.0.0.1:9/steal"}, {}))
    with pytest.raises(sdk.KebivoHTTPError) as caught:
        sdk.Kebivo(url, "token").report_gap("Question?")
    assert caught.value.status_code == 302
    assert len(received) == 1


def test_retry_after_is_capped(stub: Any, monkeypatch: Any) -> None:
    url, _received, replies = stub
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    replies.extend(
        [
            (429, {"Retry-After": "86400"}, {"error": {"code": "rate_limited"}}),
            (202, {}, {"event_id": "event-1"}),
        ]
    )
    sdk.Kebivo(url, "token").report_gap("Question?")
    assert waits == [sdk.MAX_RETRY_AFTER]
