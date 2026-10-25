"""Group read API filters, pagination, search safety and access."""

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebivo.feedback.groups import create_initial_group
from kebivo.models import (
    Delivery,
    Destination,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    Tag,
)
from kebivo.tests.test_feedback_submission import setup


def seeded(
    tmp_path: Path,
) -> tuple[TestClient, FastAPI, dict[str, str], list[tuple[str, str, int]]]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'feedback.db'}")
    command.upgrade(config, "head")
    client, app, project, _ = setup(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ids = []
    with app.state.session_factory() as db:
        it = Tag(project_id=project["id"], name="IT", slug="it")
        hr = Tag(project_id=project["id"], name="HR", slug="hr")
        db.add_all([it, hr])
        db.flush()
        for index in range(9):
            event = FeedbackEvent(
                id=str(uuid4()),
                project_id=project["id"],
                submitted_via="rest",
                event_type="knowledge_gap" if index % 2 == 0 else "knowledge_correction",
                question=f"alpha record {index}",
                correction="correct answer" if index % 2 else None,
                normalized_question_hash="a" * 64,
                created_at=start + timedelta(minutes=index // 2),
            )
            db.add(event)
            db.flush()
            group = create_initial_group(db, event)
            group.status = ("open", "in_progress", "resolved", "dismissed")[index % 4]
            if index % 2 == 0:
                db.add(FeedbackEventTag(event_id=event.id, tag_id=it.id, from_admin=True))
            if index % 3 == 0:
                db.add(FeedbackEventTag(event_id=event.id, tag_id=hr.id, from_submission=True))
            ids.append((group.id, event.id, index))
        db.commit()
    return client, app, project, ids


def test_group_filters_search_sort_and_cursor(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    path = "/api/v1/admin/groups"
    expected = {group_id for group_id, _, index in ids if index % 6 == 0}
    cases: list[tuple[dict[str, str | int | list[str]], set[str]]] = [
        (
            {
                "project_id": project["id"],
                "event_type": "knowledge_gap",
                "status": ["open", "resolved"],
                "tag": ["it", "hr"],
            },
            expected,
        ),
        ({"tag": ["it", "hr"]}, expected),
        ({"tag": [" IT "]}, {group_id for group_id, _, index in ids if index % 2 == 0}),
        ({"tag": ["IT", "HR"]}, expected),
        ({"q": "alpha rec"}, {group_id for group_id, _, _ in ids}),
        (
            {"last_seen_from": "2026-01-01T00:03:00Z"},
            {group_id for group_id, _, index in ids if index >= 6},
        ),
        (
            {"last_seen_from": "2026-01-01T05:03:00+05:00"},
            {group_id for group_id, _, index in ids if index >= 6},
        ),
        ({"min_occurrences": 2}, set()),
    ]
    for params, wanted in cases:
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["items"]} == wanted, params
    for sort in (
        "last_activity_desc",
        "last_activity_asc",
        "occurrences_desc",
        "occurrences_asc",
        "status",
        "first_reported_desc",
    ):
        seen: list[str] = []
        cursor = None
        for _ in range(3):
            params = {"sort": sort, "limit": 3}
            if cursor:
                params["cursor"] = cursor
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            seen.extend(item["id"] for item in response.json()["items"])
            cursor = response.json()["next_cursor"]
        assert len(seen) == len(set(seen)) == 9
        assert cursor is None
    first = client.get(path, params={"limit": 1}).json()["next_cursor"]
    assert first is not None
    cursor_data = json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
    timestamp = datetime.fromisoformat(cursor_data["v"][0]).replace(tzinfo=UTC)
    cursor_data["v"][0] = timestamp.astimezone(timezone(timedelta(hours=5))).isoformat()
    offset_cursor = base64.urlsafe_b64encode(
        json.dumps(cursor_data, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    expected_page = client.get(path, params={"limit": 1, "cursor": first})
    offset_page = client.get(path, params={"limit": 1, "cursor": offset_cursor})
    assert offset_page.status_code == 200
    assert offset_page.json()["items"] == expected_page.json()["items"]
    mismatch = client.get(path, params={"limit": 1, "tag": "it", "cursor": first})
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "invalid_cursor"
    assert client.get(path, params={"tag": "!invalid"}).status_code == 422
    for sort in ("occurrences_desc", "status"):
        first_page = client.get(path, params={"sort": sort, "limit": 1}).json()
        original = json.loads(
            base64.urlsafe_b64decode(
                first_page["next_cursor"] + "=" * (-len(first_page["next_cursor"]) % 4)
            )
        )
        for value in (-1, 2**31, 10**30):
            forged = {**original, "v": [value, *original["v"][1:]]}
            forged_cursor = (
                base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
            )
            invalid = client.get(path, params={"sort": sort, "cursor": forged_cursor})
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "invalid_cursor"
    assert client.get(path, params={"q": "!!!"}).status_code == 422
    assert client.get(path, params={"q": "a" * 201}).status_code == 422
    assert client.get(path, params=[("tag", "it")] * 11).status_code == 422
    for query in ('"', "*", "NEAR", "AND OR NOT", "-", "^", "question:alpha", '"* NEAR ^'):
        assert client.get(path, params={"q": query}).status_code in (200, 422)
    app.state.engine.dispose()


def test_tag_filter_normalizes_unicode_and_supported_punctuation(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    with app.state.session_factory() as db:
        for slug in ("café", "ops-team"):
            tag = Tag(project_id=project["id"], name=slug, slug=slug)
            db.add(tag)
            db.flush()
            db.add(FeedbackEventTag(event_id=ids[0][1], tag_id=tag.id, from_admin=True))
        db.commit()
    for value in (" CAFÉ ", "Ops--Team"):
        response = client.get("/api/v1/admin/groups", params={"tag": value})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [ids[0][0]]
    assert client.get("/api/v1/admin/groups", params={"tag": "ops!team"}).status_code == 422
    app.state.engine.dispose()


def test_summary_aggregates_counts_and_limits_top_gaps(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    with app.state.session_factory() as db:
        for index, (group_id, _, _) in enumerate(ids):
            group = db.get(FeedbackGroup, group_id)
            assert group is not None
            group.status = "open"
            group.occurrence_count = index + 1
        db.commit()
    response = client.get("/api/v1/admin/groups/summary", params={"project_id": project["id"]})
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["total_groups"] == 9
    assert summary["total_events"] == 45
    assert summary["groups_by_status"] == {
        "open": 9, "in_progress": 0, "resolved": 0, "dismissed": 0
    }
    assert [item["id"] for item in summary["top_open_gaps"]] == [
        ids[index][0] for index in (8, 6, 4, 2, 0)
    ]
    app.state.engine.dispose()


def test_group_read_routes_are_admin_only(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    group_id = ids[0][0]
    paths = (
        "/api/v1/admin/groups",
        "/api/v1/admin/groups/summary",
        f"/api/v1/admin/groups/{group_id}",
        f"/api/v1/admin/groups/{group_id}/events",
        f"/api/v1/admin/groups/{group_id}/history",
        "/api/v1/deliveries?group_id=" + group_id,
    )
    for path in paths:
        response = client.get(path, headers={"Authorization": "Bearer project-token"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["cache-control"] == "no-store"
    detail = client.get(f"/api/v1/admin/groups/{group_id}")
    assert detail.status_code == 200
    assert detail.json()["representative"]["question"] == "alpha record 0"
    events = client.get(f"/api/v1/admin/groups/{group_id}/events").json()["items"]
    assert next(tag for tag in events[0]["tags"] if tag["slug"] == "it")["from_admin"] is True
    assert events[0]["agent_reported"]["question"] == "alpha record 0"
    assert (
        client.get(f"/api/v1/admin/groups/{group_id}/history").json()["items"][0]["action"]
        == "created"
    )
    with app.state.session_factory() as db:
        destination = Destination(name="Test inbox", type="inbox")
        db.add(destination)
        db.flush()
        group = db.get(FeedbackGroup, group_id)
        assert group is not None
        db.add(
            Delivery(
                event_id=ids[0][1],
                project_id=group.project_id,
                destination_id=destination.id,
                status="delivered",
            )
        )
        db.commit()
    deliveries = client.get("/api/v1/deliveries", params={"group_id": group_id})
    assert deliveries.status_code == 200
    assert [item["event_id"] for item in deliveries.json()["items"]] == [ids[0][1]]
    assert (
        client.get(f"/api/v1/admin/groups/{group_id}").json()["delivery_summary"]["delivered"] == 1
    )
    summary = client.get("/api/v1/admin/groups/summary").json()
    assert summary["total_groups"] == summary["total_events"] == 9
    assert summary["groups_by_status"] == {
        "open": 3,
        "in_progress": 2,
        "resolved": 2,
        "dismissed": 2,
    }
    app.state.engine.dispose()
