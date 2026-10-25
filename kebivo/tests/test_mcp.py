"""Real HTTP MCP client coverage for both supported protocol eras."""

import asyncio
import json
import logging
import socket
import subprocess
import threading
import time
from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import func, select

from kebivo.app import create_app
from kebivo.auth import create_admin
from kebivo.feedback.ingest import InvalidSubmission
from kebivo.feedback_limits import FixedWindowLimiter
from kebivo.mcp_server import CORRECTION, GAP
from kebivo.models import (
    ApiToken,
    FeedbackEvent,
    FeedbackEventTag,
    IdempotencyRecord,
    ProcessingJob,
    Project,
    Tag,
    now_utc,
)
from kebivo.settings import Settings, SettingsError
from kebivo.tests.test_projects import make_client
from kebivo.tests.test_tokens import create_project, issue_token
from kebivo.tokens import CORRECTION_SCOPE, GAP_SCOPE


@pytest.fixture
def running(tmp_path: Path) -> Generator[tuple[str, Any, dict[str, Any], str, str]]:
    path = tmp_path / "mcp.sqlite3"
    subprocess.run(
        [".venv/bin/alembic", "upgrade", "head"],
        env={"DATABASE_URL": "sqlite:///" + str(path), "APP_ENV": "test"},
        check=True,
        capture_output=True,
    )
    settings = Settings(
        "Kebivo", "test", "project-tests-secret-key-000000000000", str(path)
    )
    app = create_app(settings)
    with app.state.session_factory() as db:
        create_admin(db, "admin@example.test", "long-test-password")
    # Admin setup uses the established REST helpers against the same file.
    admin, _ = make_client(path)
    project = create_project(admin, "mcp", corrections=True)
    gap = issue_token(admin, project["id"], "gap").json()["token"]
    both = issue_token(admin, project["id"], "both", [GAP_SCOPE, CORRECTION_SCOPE]).json()["token"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.03)
    assert server.started
    yield f"http://127.0.0.1:{port}", app, project, gap, both
    server.should_exit = True
    thread.join(timeout=5)


async def _exercise(
    url: str, app: Any, project: dict[str, Any], gap: str, both: str, version: str
) -> None:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
        async with streamable_http_client(
            url + "/mcp", http_client=http, terminate_on_close=False
        ) as streams:
            async with ClientSession(*streams[:2]) as session:
                if version == "2026-07-28":
                    await session.discover()
                else:
                    await session.initialize()
                assert session.protocol_version == version
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    "report_knowledge_gap",
                    "submit_knowledge_correction",
                }
                result = await session.call_tool(
                    "report_knowledge_gap", {"question": "VPN procedure?", "tags": ["IT"]}
                )
                assert result.structured_content["status"] == "accepted"
                assert result.content[0].type == "text"
                assert json.loads(result.content[0].text) == result.structured_content
                event_id = result.structured_content["event_id"]
                with app.state.session_factory() as db:
                    event = db.get(FeedbackEvent, event_id)
                    assert event is not None and event.submitted_via == "mcp"
                correction = await session.call_tool(
                    "submit_knowledge_correction",
                    {"question": "Warranty?", "correction": "36 months"},
                )
                assert correction.structured_content["verification_status"] == "unverified"
                accepted_counts = _counts(app)
                invalid = await session.call_tool("report_knowledge_gap", {"question": ""})
                assert (
                    invalid.is_error
                    and invalid.structured_content["error"]["code"] == "validation_error"
                )
                unknown = await session.call_tool(
                    "report_knowledge_gap", {"question": "x", "metadata": {"a": 1}}
                )
                assert (
                    unknown.is_error
                    and unknown.structured_content["error"]["fields"][0]["field"] == "metadata"
                )
                assert _counts(app) == accepted_counts
                for field, value in (
                    ("question", "x" * 10001),
                    ("question", "bad\x00value"),
                    ("tags", ["x" * 65]),
                ):
                    response = await session.call_tool(GAP, {"question": "valid", field: value})
                    assert response.is_error
                    assert response.structured_content["error"]["code"] == "validation_error"
                    assert _counts(app) == accepted_counts
                key = f"mcp-{version}"
                first = await session.call_tool(GAP, {"question": "retry", "idempotency_key": key})
                replay = await session.call_tool(GAP, {"question": "retry", "idempotency_key": key})
                assert replay.structured_content["event_id"] == first.structured_content["event_id"]
                assert replay.structured_content["replayed"] is True
                conflict = await session.call_tool(
                    GAP, {"question": "different", "idempotency_key": key}
                )
                assert conflict.is_error
                assert conflict.structured_content["error"]["code"] == "idempotency_conflict"
                with app.state.session_factory() as db:
                    project_row = db.get(Project, project["id"])
                    assert project_row is not None
                    project_row.settings_json = json.dumps({"corrections": {"enabled": False}})
                    db.commit()
                assert [tool.name for tool in (await session.list_tools()).tools] == [GAP]
                with pytest.raises(MCPError) as disabled:
                    await session.call_tool(CORRECTION, {"question": "x", "correction": "y"})
                with pytest.raises(MCPError) as unknown_name:
                    await session.call_tool("unknown_tool", {})
                assert disabled.value.code == unknown_name.value.code == -32602
                assert str(disabled.value) == str(unknown_name.value).replace(
                    "unknown_tool", CORRECTION
                )
                with app.state.session_factory() as db:
                    project_row = db.get(Project, project["id"])
                    assert project_row is not None
                    project_row.settings_json = json.dumps({"corrections": {"enabled": True}})
                    db.commit()
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {gap}"}) as http:
        async with streamable_http_client(
            url + "/mcp", http_client=http, terminate_on_close=False
        ) as streams:
            async with ClientSession(*streams[:2]) as session:
                if version == "2026-07-28":
                    await session.discover()
                else:
                    await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == [
                    "report_knowledge_gap"
                ]
                with pytest.raises(MCPError) as hidden:
                    await session.call_tool(CORRECTION, {"question": "x", "correction": "y"})
                assert hidden.value.code == -32602


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_real_client(running: tuple[str, Any, dict[str, Any], str, str], version: str) -> None:
    asyncio.run(_exercise(*running, version))


def test_http_rejections(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, _, _, both = running
    with app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(FeedbackEvent))
    with httpx.Client() as client:
        assert client.post(url + "/mcp", content=b"{}").status_code == 401
        assert (
            client.post(
                url + "/mcp", content=b"x" * 131073, headers={"Authorization": f"Bearer {both}"}
            ).status_code
            == 413
        )
        assert (
            client.post(
                url + "/mcp",
                json={},
                headers={"Authorization": f"Bearer {both}", "Host": "evil.example"},
            ).status_code
            == 421
        )
        assert (
            client.post(
                url + "/mcp",
                json={},
                headers={"Authorization": f"Bearer {both}", "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get(url + "/.well-known/oauth-protected-resource").status_code == 404
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == before


@pytest.mark.parametrize("accept", ["text/event-stream", None])
def test_get_never_opens_stream(
    running: tuple[str, Any, dict[str, Any], str, str], accept: str | None
) -> None:
    url, _, _, _, both = running
    with httpx.Client(timeout=2) as client:
        request = client.build_request(
            "GET", url + "/mcp", headers={"Authorization": f"Bearer {both}"}
        )
        if accept is None:
            request.headers.pop("accept", None)
        else:
            request.headers["accept"] = accept
        response = client.send(request)
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.headers["content-type"].startswith("application/json")

        anonymous = client.get(url + "/mcp", headers={"Accept": accept or "*/*"})
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == "Bearer"

        deleted = client.delete(url + "/mcp", headers={"Authorization": f"Bearer {both}"})
        assert deleted.status_code == 405
        assert deleted.headers["allow"] == "POST"


def _counts(app: Any) -> tuple[int, ...]:
    with app.state.session_factory() as db:
        return tuple(
            int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in (FeedbackEvent, FeedbackEventTag, ProcessingJob, IdempotencyRecord, Tag)
        )


def _mcp_post(
    url: str, token: str | None, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    supplied = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        supplied["Authorization"] = f"Bearer {token}"
    supplied.update(headers or {})
    return httpx.post(
        url + "/mcp", content=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=supplied
    )


def test_auth_lifecycle_and_zero_rows(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, project, gap, both = running
    original = _counts(app)
    for token in (None, "malformed", "kebivo_pt_unknown.invalid"):
        response = _mcp_post(url, token)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "resource_metadata" not in str(response.headers)
        assert _counts(app) == original
    with app.state.session_factory() as db:
        row = db.scalar(
            select(ApiToken).where(ApiToken.project_id == project["id"], ApiToken.name == "gap")
        )
        assert row is not None
        row.revoked_at = now_utc()
        db.commit()
    assert _mcp_post(url, gap).status_code == 401
    with app.state.session_factory() as db:
        row = db.scalar(
            select(ApiToken).where(ApiToken.project_id == project["id"], ApiToken.name == "gap")
        )
        assert row is not None
        row.revoked_at = None
        row.expires_at = now_utc() - timedelta(seconds=1)
        db.commit()
    assert _mcp_post(url, gap).status_code == 401
    with app.state.session_factory() as db:
        row = db.get(Project, project["id"])
        assert row is not None
        row.is_active = False
        db.commit()
    assert _mcp_post(url, both).status_code == 401
    with app.state.session_factory() as db:
        row = db.get(Project, project["id"])
        assert row is not None
        row.is_active = True
        row.archived_at = now_utc()
        db.commit()
    assert _mcp_post(url, both).status_code == 401
    assert _mcp_post(url, None, headers={"Cookie": "kebivo_admin_session=only"}).status_code == 401
    assert _counts(app) == original


def test_ip_limit_and_disabled_endpoint(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, _, _, both = running
    app.state.feedback_limiter = FixedWindowLimiter()
    app.state.settings = replace(app.state.settings, feedback_rate_limit_per_ip=1)
    assert _mcp_post(url, both).status_code == 200
    denied = _mcp_post(url, both)
    assert denied.status_code == 429 and int(denied.headers["retry-after"]) >= 1
    app.state.settings = replace(app.state.settings, mcp_enabled=False)
    # Disabling via configuration is a startup choice; a new app has no MCP route.
    disabled = create_app(app.state.settings)
    assert all(getattr(route, "path", None) != "/mcp" for route in disabled.routes)
    disabled.state.engine.dispose()


def test_rest_mcp_shared_idempotency(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, _, _, both = running
    payload = {"event_type": "knowledge_gap", "question": "Shared report", "tags": ["IT"]}
    rest = httpx.post(
        url + "/api/v1/feedback",
        json=payload,
        headers={"Authorization": f"Bearer {both}", "Idempotency-Key": "cross-channel"},
    )
    assert rest.status_code == 202

    async def replay() -> Any:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    return await session.call_tool(
                        GAP,
                        {
                            "question": "Shared report",
                            "tags": ["IT"],
                            "idempotency_key": "cross-channel",
                        },
                    )

    result = asyncio.run(replay())
    assert result.structured_content["event_id"] == rest.json()["event_id"]
    assert result.structured_content["replayed"] is True
    assert _counts(app)[:4] == (1, 1, 1, 1)


def test_rest_and_mcp_store_equivalent_fields(
    running: tuple[str, Any, dict[str, Any], str, str],
) -> None:
    url, app, _, _, both = running
    fields = {
        "question": "Same question",
        "previous_answer": "Old reply",
        "context": "Missing approved source",
        "tags": ["IT", "help-desk"],
        "source_event_id": "message-1",
        "conversation_id": "thread-1",
        "agent_id": "assistant-1",
    }
    rest = httpx.post(
        url + "/api/v1/feedback",
        json={"event_type": "knowledge_gap", **fields},
        headers={"Authorization": f"Bearer {both}"},
    )
    assert rest.status_code == 202

    async def submit() -> Any:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    return await session.call_tool(GAP, fields)

    mcp = asyncio.run(submit())
    assert mcp.structured_content["status"] == "accepted"
    with app.state.session_factory() as db:
        rest_event = db.get(FeedbackEvent, rest.json()["event_id"])
        mcp_event = db.get(FeedbackEvent, mcp.structured_content["event_id"])
        assert rest_event is not None and mcp_event is not None
        assert rest_event.submitted_via == "rest" and mcp_event.submitted_via == "mcp"
        for field in (
            "project_id",
            "api_token_id",
            "event_type",
            "question",
            "previous_answer",
            "context",
            "source_event_id",
            "conversation_id",
            "agent_id",
            "metadata_json",
            "normalized_question_hash",
            "question_hash_version",
        ):
            assert getattr(rest_event, field) == getattr(mcp_event, field)
        assert rest_event.processing_status in {"pending", "processing", "completed"}
        assert mcp_event.processing_status in {"pending", "processing", "completed"}

        def links(event_id: str) -> set[tuple[str, bool, bool]]:
            return {
                (tag.slug, link.from_default, link.from_submission)
                for link, tag in db.execute(
                    select(FeedbackEventTag, Tag)
                    .join(Tag, Tag.id == FeedbackEventTag.tag_id)
                    .where(FeedbackEventTag.event_id == event_id)
                )
            }

        assert links(rest_event.id) == links(mcp_event.id)
        assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 2


def test_surrogate_rejected_over_mcp(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, _, _, both = running
    before = _counts(app)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": GAP, "arguments": {"question": "bad\ud800value"}},
        }
    ).encode("ascii")
    response = httpx.post(
        url + "/mcp",
        content=body,
        headers={
            "Authorization": f"Bearer {both}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    # The SDK JSON parser rejects an unpaired surrogate before tool dispatch.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
    assert _counts(app) == before


def test_invalid_allowed_host_fails_startup() -> None:
    with pytest.raises(SettingsError, match="MCP_ALLOWED_HOSTS"):
        Settings.from_env({"MCP_ALLOWED_HOSTS": "https://bad.example/path"})


def test_token_limit_is_tool_error(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    url, app, _, _, both = running
    app.state.settings = replace(app.state.settings, feedback_rate_limit_per_token=1)

    async def call_twice() -> tuple[Any, Any]:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    return (
                        await session.call_tool(GAP, {"question": "first"}),
                        await session.call_tool(GAP, {"question": "second"}),
                    )

    first, second = asyncio.run(call_twice())
    assert first.structured_content["status"] == "accepted"
    assert second.is_error
    assert second.structured_content["error"]["code"] == "rate_limited"
    assert second.structured_content["error"]["retry_after_seconds"] >= 1
    assert _counts(app)[0] == 1


def test_twenty_one_tags_rejected_without_writes(
    running: tuple[str, Any, dict[str, Any], str, str],
) -> None:
    url, app, _, _, both = running
    before = _counts(app)

    async def submit() -> tuple[Any, Any]:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    schema = (await session.list_tools()).tools[0].input_schema
                    result = await session.call_tool(
                        GAP, {"question": "Too many tags", "tags": [f"tag{i}" for i in range(21)]}
                    )
                    return schema, result

    schema, result = asyncio.run(submit())
    assert schema["properties"]["tags"]["maxItems"] == 20
    assert result.is_error
    assert result.structured_content["error"]["code"] == "validation_error"
    assert result.structured_content["error"]["fields"][0]["field"] == "tags"
    assert _counts(app) == before


def test_permission_read_failure_hides_sql(
    running: tuple[str, Any, dict[str, Any], str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, app, _, _, both = running
    before = _counts(app)
    secret = "SELECT private_data FROM secret_table"

    def fail_permissions(*_args: Any) -> Any:
        def fail() -> Any:
            raise RuntimeError(secret)

        return fail

    monkeypatch.setattr("kebivo.mcp_server._permissions", fail_permissions)
    headers = {
        "Authorization": f"Bearer {both}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with caplog.at_level(logging.WARNING):
        responses = [
            httpx.post(
                url + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": method,
                    **(
                        {"params": {"name": GAP, "arguments": {"question": "safe"}}}
                        if number == 2
                        else {}
                    ),
                },
                headers=headers,
            )
            for number, method in ((1, "tools/list"), (2, "tools/call"))
        ]
    assert all(response.status_code == 200 for response in responses)
    listed, called = [response.json() for response in responses]
    assert listed["error"]["code"] == -32603
    assert "internal_error request_id=" in listed["error"]["message"]
    assert called["result"]["structuredContent"]["error"]["code"] == "internal_error"
    assert called["result"]["structuredContent"]["error"]["request_id"]
    captured = caplog.text + str(listed) + str(called)
    assert secret not in captured
    assert "secret_table" not in captured
    assert _counts(app) == before


def test_model_validation_without_location_has_neutral_field(
    running: tuple[str, Any, dict[str, Any], str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _, _, _, both = running

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise InvalidSubmission([{"loc": (), "type": "value_error"}])

    monkeypatch.setattr("kebivo.mcp_server.ingest", fail)

    async def submit() -> Any:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    return await session.call_tool(CORRECTION, {"question": "q", "correction": "c"})

    result = asyncio.run(submit())
    assert result.is_error
    assert result.structured_content["error"]["fields"][0]["field"] is None


def test_unexpected_error_and_logs_hide_values(
    running: tuple[str, Any, dict[str, Any], str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, app, _, _, both = running
    marker = "private-question-marker"
    before = _counts(app)

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("private-exception-marker")

    monkeypatch.setattr("kebivo.mcp_server.ingest", fail)

    async def call() -> Any:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {both}"}) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.discover()
                    return await session.call_tool(GAP, {"question": marker})

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(call())
    assert result.is_error
    assert result.structured_content["error"]["code"] == "internal_error"
    assert result.structured_content["error"]["request_id"]
    captured = caplog.text + str(result.structured_content)
    assert marker not in captured
    assert both not in captured
    assert "private-exception-marker" not in captured
    assert _counts(app) == before
