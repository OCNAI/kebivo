"""Builders can see whether a resolved group stayed quiet or came back."""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kebivo.tests.test_feedback_submission import post, setup
from kebivo.tests.test_projects import write_headers

QUESTION = {"event_type": "knowledge_gap", "question": "Do you ship to Canada?"}


def _group(client: TestClient, project_id: str, token: str) -> str:
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={"settings": {"deduplication": {"mode": "exact"}}},
        headers=write_headers(client),
    )
    event_id = post(client, token, QUESTION).json()["event_id"]
    group = client.get(f"/api/v1/admin/feedback/{event_id}").json()["group"]
    return str(group["id"])


def _row(client: TestClient, group_id: str, **params: Any) -> dict[str, Any] | None:
    items = client.get("/api/v1/admin/groups", params=params).json()["items"]
    return next((item for item in items if item["id"] == group_id), None)


def _resolve(client: TestClient, group_id: str, note: str | None = None) -> None:
    path = f"/api/v1/admin/groups/{group_id}"
    assert client.patch(path, json={"status": "resolved"}, headers=write_headers(client)).is_success
    if note is not None:
        response = client.post(
            f"{path}/notes", json={"body": note, "resolution": True}, headers=write_headers(client)
        )
        assert response.status_code == 201


def test_resolved_group_stays_quiet_with_its_note(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    group_id = _group(client, project["id"], token)
    _resolve(client, group_id, "Added shipping countries to the FAQ")
    row = _row(client, group_id, status="resolved")
    assert row is not None and row["resolved_at"] and row["reopened"] is None
    assert row["resolution_note"]["body"] == "Added shipping countries to the FAQ"
    detail = client.get(f"/api/v1/admin/groups/{group_id}").json()
    assert detail["resolution_note"]["id"] == row["resolution_note"]["id"]
    assert _row(client, group_id, reopened="true") is None
    summary = client.get("/api/v1/admin/groups/summary").json()
    assert (summary["resolved_last_7d"], summary["reopened_last_7d"], summary["reopened"]) == (
        1,
        0,
        0,
    )
    assert _row(client, group_id, status="resolved", status_changed_from=summary["last_7d_from"])
    app.state.engine.dispose()


def test_resolved_then_reopened_by_recurrence(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    group_id = _group(client, project["id"], token)
    _resolve(client, group_id, "Linked the shipping page")
    resolved_at = client.get(f"/api/v1/admin/groups/{group_id}").json()["resolved_at"]
    post(client, token, QUESTION)
    post(client, token, QUESTION)
    row = _row(client, group_id, reopened="true")
    assert row is not None and row["status"] == "open" and row["resolved_at"] is None
    assert row["reopened"]["new_occurrences"] == 2
    assert row["reopened"]["resolved_at"] <= row["reopened"]["reopened_at"]
    assert row["reopened"]["resolved_at"][:19] == resolved_at[:19]
    assert row["resolution_note"]["body"] == "Linked the shipping page"
    summary = client.get("/api/v1/admin/groups/summary").json()
    assert (summary["reopened"], summary["reopened_last_7d"], summary["resolved_last_7d"]) == (
        1,
        1,
        0,
    )
    assert summary["groups_by_status"]["open"] == 1
    # Once an administrator triages it again it is ordinary work, not a reopen.
    assert client.patch(
        f"/api/v1/admin/groups/{group_id}",
        json={"status": "in_progress"},
        headers=write_headers(client),
    ).is_success
    assert _row(client, group_id, reopened="true") is None
    assert _row(client, group_id)["reopened"] is None  # type: ignore[index]
    app.state.engine.dispose()


def test_note_on_resolve_is_optional_and_needs_a_resolved_group(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    group_id = _group(client, project["id"], token)
    notes = f"/api/v1/admin/groups/{group_id}/notes"
    early = client.post(
        notes, json={"body": "too early", "resolution": True}, headers=write_headers(client)
    )
    assert (early.status_code, early.json()["error"]["code"]) == (409, "not_resolved")
    _resolve(client, group_id)
    row = _row(client, group_id)
    assert row is not None and row["status"] == "resolved" and row["resolution_note"] is None
    # An ordinary note is not shown as the resolution note.
    client.post(notes, json={"body": "plain note"}, headers=write_headers(client))
    assert _row(client, group_id)["resolution_note"] is None  # type: ignore[index]
    note = client.post(
        notes, json={"body": "Rewrote the FAQ", "resolution": True}, headers=write_headers(client)
    ).json()
    assert _row(client, group_id)["resolution_note"]["id"] == note["id"]  # type: ignore[index]
    client.delete(f"{notes}/{note['id']}", headers=write_headers(client))
    assert _row(client, group_id)["resolution_note"] is None  # type: ignore[index]
    app.state.engine.dispose()


def test_merging_a_reopened_group_does_not_mark_the_target_reopened(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    other = post(client, token, {"event_type": "knowledge_gap", "question": "Is there a VPN?"})
    target = client.get(f"/api/v1/admin/feedback/{other.json()['event_id']}").json()["group"]["id"]
    source = _group(client, project["id"], token)
    _resolve(client, source)
    post(client, token, QUESTION)
    assert _row(client, source, reopened="true") is not None
    merged = client.post(
        f"/api/v1/admin/groups/{target}/merge",
        json={"source_group_ids": [source]},
        headers=write_headers(client),
    )
    assert merged.is_success
    assert _row(client, target, reopened="true") is None
    assert _row(client, target)["reopened"] is None  # type: ignore[index]
    app.state.engine.dispose()
