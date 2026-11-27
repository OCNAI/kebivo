"""Project-token gap reads: scope, field allow-list, paging, limits and audit (ADR A9)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select

from kebivo.feedback.groups import create_initial_group
from kebivo.mcp_server import LIST_GAPS
from kebivo.models import (
    AuditEvent,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroupNote,
    Tag,
)
from kebivo.tests.test_mcp import running  # noqa: F401
from kebivo.tests.test_projects import make_client
from kebivo.tests.test_tokens import create_project, issue_token
from kebivo.tokens import CORRECTION_SCOPE, GAP_SCOPE, GAPS_READ_SCOPE

START = datetime(2026, 1, 1, tzinfo=UTC)
FIELDS = {"id", "question", "occurrence_count", "first_seen_at", "last_seen_at", "tags", "status"}


def seed(app: FastAPI, project_id: str, rows: list[tuple[str, str, int, int]]) -> list[str]:
    """rows: (question, status, occurrence_count, minutes after START for last_seen)."""
    ids = []
    with app.state.session_factory() as db:
        tag = db.scalar(select(Tag).where(Tag.project_id == project_id, Tag.slug == "billing"))
        if tag is None:
            tag = Tag(project_id=project_id, name="Billing", slug="billing")
            db.add(tag)
            db.flush()
        for question, status, count, minutes in rows:
            event = FeedbackEvent(
                id=str(uuid4()),
                project_id=project_id,
                submitted_via="rest",
                event_type="knowledge_correction" if status == "correction" else "knowledge_gap",
                question=question,
                correction="secret correction" if status == "correction" else None,
                context="private context",
                user_reference="user-42@example.test",
                normalized_question_hash=str(uuid4()).replace("-", "") * 2,
                created_at=START,
            )
            db.add(event)
            db.flush()
            group = create_initial_group(db, event)
            group.status = "open" if status == "correction" else status
            group.occurrence_count = count
            group.last_seen_at = START + timedelta(minutes=minutes)
            db.add(FeedbackEventTag(event_id=event.id, tag_id=tag.id, from_submission=True))
            db.add(FeedbackGroupNote(group_id=group.id, body="internal note"))
            ids.append(group.id)
        db.commit()
    return ids


def setup(tmp_path: Path, **settings: Any) -> tuple[TestClient, FastAPI, dict[str, Any], str]:
    client, app = make_client(tmp_path / "gaps.db")
    if settings:
        app.state.settings = replace(app.state.settings, **settings)
    project = create_project(client, "gaps")
    token = issue_token(client, project["id"], "reader", [GAPS_READ_SCOPE])
    assert token.status_code == 201 and token.json()["scopes"] == [GAPS_READ_SCOPE]
    return client, app, project, token.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_scope_is_opt_in_and_read_only_tokens_are_allowed(tmp_path: Path) -> None:
    client, app, project, reader = setup(tmp_path)
    default = issue_token(client, project["id"], "default").json()
    assert default["scopes"] == [GAP_SCOPE]
    assert issue_token(client, project["id"], "bad", [CORRECTION_SCOPE]).status_code == 422
    assert issue_token(client, project["id"], "all", [GAP_SCOPE, GAPS_READ_SCOPE]).json()[
        "scopes"
    ] == [GAP_SCOPE, GAPS_READ_SCOPE]
    anonymous = TestClient(app)
    assert anonymous.get("/api/v1/groups").status_code == 401
    assert anonymous.get("/api/v1/groups", headers=bearer("kebivo_pt_x.y")).status_code == 401
    denied = anonymous.get("/api/v1/groups", headers=bearer(default["token"]))
    assert denied.status_code == 403
    error = denied.json()["error"]
    assert error["code"] == "scope_required"
    assert "gaps:read" in error["message"]
    assert error["details"] == {"required_scope": "gaps:read"}
    assert error["request_id"] == denied.headers["x-request-id"]
    # The admin session alone is not a credential for this route.
    assert client.get("/api/v1/groups").status_code == 401
    # A read-only token cannot submit feedback.
    submit = anonymous.post(
        "/api/v1/feedback",
        json={"event_type": "knowledge_gap", "question": "x"},
        headers=bearer(reader),
    )
    assert submit.status_code == 403
    app.state.engine.dispose()


def test_fields_are_allow_listed_and_scoped_to_project(tmp_path: Path) -> None:
    client, app, project, reader = setup(tmp_path)
    other = create_project(client, "other")
    seed(app, other["id"], [("other project question", "open", 9, 1)])
    seed(app, project["id"], [("q" * 1500, "open", 3, 1), ("a correction", "correction", 50, 2)])
    response = TestClient(app).get("/api/v1/groups", headers=bearer(reader))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    [item] = body["items"]
    assert set(item) == FIELDS
    assert item["question"] == "q" * 1000
    assert item["tags"] == ["billing"] and item["status"] == "open"
    assert item["occurrence_count"] == 3
    text = response.text
    for secret in ("internal note", "user-42", "private context", "secret correction", "other"):
        assert secret not in text
    app.state.engine.dispose()


def test_status_sort_and_cursor_paging(tmp_path: Path) -> None:
    _, app, project, reader = setup(tmp_path)
    ids = seed(
        app,
        project["id"],
        [
            ("low recent", "open", 1, 30),
            ("high old", "open", 9, 1),
            ("mid", "open", 5, 10),
            ("tie later", "open", 5, 20),
            ("done", "resolved", 99, 40),
        ],
    )
    api = TestClient(app)
    listed = api.get("/api/v1/groups", headers=bearer(reader)).json()["items"]
    order = [item["question"] for item in listed]
    assert order == ["high old", "tie later", "mid", "low recent"]
    recent = api.get("/api/v1/groups?sort=last_seen", headers=bearer(reader)).json()["items"]
    assert [item["question"] for item in recent] == ["low recent", "tie later", "mid", "high old"]
    both = api.get("/api/v1/groups?status=open&status=resolved", headers=bearer(reader))
    assert both.json()["items"][0]["id"] == ids[4]
    seen = []
    cursor = None
    while True:
        query = "/api/v1/groups?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = api.get(query, headers=bearer(reader)).json()
        seen += [item["question"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == order
    first = api.get("/api/v1/groups?limit=1", headers=bearer(reader)).json()["next_cursor"]
    wrong_sort = api.get(f"/api/v1/groups?sort=last_seen&cursor={first}", headers=bearer(reader))
    assert wrong_sort.status_code == 400
    # Every invalid filter returns the same 422 shape, naming the field.
    for query in ("status=closed", "sort=random", "limit=0", "limit=51", "cursor=" + "a" * 1025):
        invalid = api.get(f"/api/v1/groups?{query}", headers=bearer(reader))
        assert invalid.status_code == 422
        error = invalid.json()["error"]
        assert error["code"] == "validation_error"
        assert [item["field"] for item in error["details"]["fields"]] == [query.split("=")[0]]
    assert api.get("/api/v1/groups?cursor=bad", headers=bearer(reader)).status_code == 400
    app.state.engine.dispose()


def test_each_read_is_audited_without_content_and_rate_limited(tmp_path: Path) -> None:
    _, app, project, reader = setup(tmp_path, gaps_read_rate_limit_per_token=2)
    seed(app, project["id"], [("audited question", "open", 2, 1)])
    api = TestClient(app)
    assert api.get("/api/v1/groups", headers=bearer(reader)).status_code == 200
    assert api.get("/api/v1/groups?sort=last_seen", headers=bearer(reader)).status_code == 200
    limited = api.get("/api/v1/groups", headers=bearer(reader))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    with app.state.session_factory() as db:
        rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "gaps.read")).all()
        assert len(rows) == 2
        assert {row.actor_type for row in rows} == {"project_token"}
        assert {row.subject_id for row in rows} == {project["id"]}
        assert json.loads(rows[0].details_json)["returned"] == 1
        assert all("audited question" not in row.details_json for row in rows)
    app.state.engine.dispose()


async def _mcp(url: str, token: str) -> tuple[list[str], Any, Any]:
    async with httpx2.AsyncClient(headers=bearer(token)) as http:
        async with streamable_http_client(
            url + "/mcp", http_client=http, terminate_on_close=False
        ) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                names = [tool.name for tool in (await session.list_tools()).tools]
                try:
                    result = await session.call_tool(LIST_GAPS, {"limit": 1})
                except MCPError as exc:
                    return names, exc, None
                extra = await session.call_tool(LIST_GAPS, {"project_id": "x"})
                return names, result, extra


def test_mcp_list_open_gaps(running: tuple[str, Any, dict[str, Any], str, str]) -> None:  # noqa: F811
    url, app, project, gap, _ = running
    seed(app, project["id"], [("mcp first", "open", 4, 1), ("mcp second", "open", 2, 2)])
    names, hidden, _ = asyncio.run(_mcp(url, gap))
    assert names == ["report_knowledge_gap"]
    assert isinstance(hidden, MCPError) and hidden.code == -32602
    # Issue a read-only token through the same app and use it over MCP.
    client, _ = make_client(Path(app.state.settings.database_path))
    reader = issue_token(client, project["id"], "mcp reader", [GAPS_READ_SCOPE]).json()["token"]
    names, result, extra = asyncio.run(_mcp(url, reader))
    assert names == [LIST_GAPS]
    content = result.structured_content
    assert [item["question"] for item in content["items"]] == ["mcp first"]
    assert set(content["items"][0]) == FIELDS and content["next_cursor"]
    assert extra.is_error and extra.structured_content["error"]["code"] == "validation_error"
    with app.state.session_factory() as db:
        audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "gaps.read")).all()
        assert [json.loads(row.details_json)["via"] for row in audit] == ["mcp"]


def test_existing_write_tool_list_unchanged_for_write_tokens(
    running: tuple[str, Any, dict[str, Any], str, str],  # noqa: F811
) -> None:
    url, _, _, _, both = running
    names, hidden, _ = asyncio.run(_mcp(url, both))
    assert names == ["report_knowledge_gap", "submit_knowledge_correction"]
    assert isinstance(hidden, MCPError)


@pytest.mark.parametrize("arguments", [{"status": ["closed"]}, {"limit": 0}, {"sort": "x"}])
def test_mcp_invalid_arguments_are_tool_errors(
    running: tuple[str, Any, dict[str, Any], str, str],  # noqa: F811
    arguments: dict[str, Any],
) -> None:
    url, app, project, _, _ = running
    client, _ = make_client(Path(app.state.settings.database_path))
    reader = issue_token(client, project["id"], "r", [GAPS_READ_SCOPE]).json()["token"]

    async def call() -> Any:
        async with httpx2.AsyncClient(headers=bearer(reader)) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http, terminate_on_close=False
            ) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.initialize()
                    return await session.call_tool(LIST_GAPS, arguments)

    result = asyncio.run(call())
    assert result.is_error
