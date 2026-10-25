"""AC-14: a Project A token cannot reach Project B, and learns nothing about it."""

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from conftest import API, Admin, gap, mcp_session, record_content, run, submit, unique

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))
from webhook_receiver import schema_errors  # noqa: E402


def export_schema() -> dict[str, Any]:
    """The JSON Schema published in docs/export.md."""
    text = (ROOT / "docs" / "export.md").read_text()
    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S)]
    return next(block for block in blocks if "$schema" in block)


def _envelope(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body["error"].pop("request_id", None)
    return response.status_code, body


def test_ac_14_project_token_is_confined_to_its_project(admin: Admin) -> None:
    project_a, project_b = admin.project(unique("AC14 A")), admin.project(unique("AC14 B"))
    for project in (project_a, project_b):
        admin.put(f"/projects/{project['id']}/default-tags", {"tags": ["shared"]})
    token_a = admin.token(project_a["id"], ["feedback:gap", "feedback:correction"])
    token_b = admin.token(project_b["id"])
    secret_question = record_content(unique("AC14 B private question"))
    key = unique("shared-key")
    event_b = gap(token_b, secret_question, key=key)
    admin.processed(event_b)

    # The same Idempotency-Key and body under token A creates A's own event.
    replay = submit(token_a, {"event_type": "knowledge_gap", "question": secret_question,
                              "tags": []}, key)
    assert replay.status_code == 202 and replay.headers.get("Idempotent-Replayed") is None
    event_a = replay.json()["event_id"]
    assert event_a != event_b
    assert admin.event(event_a)["system"]["project"]["id"] == project_a["id"]

    # Admin and other-project routes reject the token exactly as for a project
    # that does not exist, so the answer reveals nothing about B.
    missing_project, missing_event = str(uuid.uuid4()), str(uuid.uuid4())
    paths = [
        "/projects/{p}", "/projects/{p}/tokens", "/projects/{p}/routing-rules",
        "/admin/projects/{p}/export", "/admin/feedback/{e}", "/admin/feedback?project_id={p}",
        "/admin/groups?project_id={p}", "/deliveries?project_id={p}", "/admin/settings",
    ]
    bearer = {"Authorization": f"Bearer {token_a}"}
    for template in paths:
        real = httpx.get(API + template.format(p=project_b["id"], e=event_b), headers=bearer)
        fake = httpx.get(API + template.format(p=missing_project, e=missing_event), headers=bearer)
        assert real.status_code in {401, 403}, (template, real.status_code)
        assert _envelope(real) == _envelope(fake), template
        assert secret_question not in real.text and project_b["name"] not in real.text
    write = httpx.post(API + f"/projects/{project_b['id']}/tokens", headers=bearer,
                       json={"name": "stolen"})
    assert write.status_code in {401, 403}

    # MCP with token A only ever writes to A and exposes no read tool.
    async def scenario() -> str:
        async with mcp_session(token_a) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
            assert names == ["report_knowledge_gap"]
            result = await session.call_tool("report_knowledge_gap", {
                "question": secret_question, "idempotency_key": key})
            return str((result.structured_content or {})["event_id"])

    mcp_event = run(scenario())
    assert mcp_event not in {event_b}
    assert admin.event(mcp_event)["system"]["project"]["id"] == project_a["id"]
    b_events = admin.get(f"/admin/feedback?project_id={project_b['id']}")["items"]
    assert [row["id"] for row in b_events] == [event_b]

    # The admin export of A contains only A's events and groups.
    export = admin.client.get(f"/admin/projects/{project_a['id']}/export")
    assert export.status_code == 200
    lines = [json.loads(line) for line in export.text.splitlines() if line]
    assert {line["id"] for line in lines} == {event_a, mcp_event}
    schema = export_schema()
    for line in lines:
        assert schema_errors(line, schema) == [], line["id"]
    assert event_b not in export.text
    b_group = admin.event(event_b)["group"]["id"]
    assert b_group not in export.text
