"""Admin feedback inbox API contract and access tests."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from kebivo.models import ApiToken, FeedbackEvent, FeedbackEventTag, Project, Tag
from kebivo.tests.test_projects import make_client


def test_feedback_inbox_order_filters_cursor_and_detail(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "inbox.db")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with app.state.session_factory() as db:
        active = Project(name="Active", slug="active")
        archived = Project(name="Archived", slug="archived", archived_at=start)
        db.add_all([active, archived])
        db.flush()
        token = ApiToken(
            project_id=active.id,
            name="Agent credential",
            token_hash="a" * 64,
            token_prefix="kebivo_pt_abc",
        )
        default_tag = Tag(project_id=active.id, name="IT", slug="it", is_default=True)
        db.add_all([token, default_tag])
        db.flush()
        ids = [str(uuid4()) for _ in range(5)]
        rows = [
            FeedbackEvent(
                id=event_id,
                project_id=archived.id if index == 0 else active.id,
                api_token_id=token.id if index == 4 else None,
                submitted_via="rest",
                event_type="knowledge_correction" if index % 2 else "knowledge_gap",
                question="😀" * 281 if index == 4 else f"Question {index}",
                correction="Corrected" if index % 2 else None,
                previous_answer="Old answer" if index == 4 else None,
                context="Context" if index == 4 else None,
                source_reference="ref" if index == 4 else None,
                agent_id="agent-7" if index == 4 else None,
                conversation_id="thread-1" if index == 4 else None,
                source_event_id="source-1" if index == 4 else None,
                user_reference="user-1" if index == 4 else None,
                metadata_json=json.dumps({"source": "test"}) if index == 4 else "{}",
                normalized_question_hash="b" * 64,
                idempotency_key="key-1" if index == 4 else None,
                created_at=start + timedelta(minutes=index // 2),
            )
            for index, event_id in enumerate(ids)
        ]
        db.add_all(rows)
        db.flush()
        db.add(
            FeedbackEventTag(
                event_id=ids[4], tag_id=default_tag.id, from_default=True, from_submission=True
            )
        )
        db.commit()

    expected = [
        row.id for row in sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)
    ]
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/admin/feedback", params=params)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        seen.extend(item["id"] for item in page.json()["items"])
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == expected
    assert len(set(seen)) == 5

    archived_page = client.get("/api/v1/admin/feedback", params={"project_id": archived.id})
    assert [item["id"] for item in archived_page.json()["items"]] == [ids[0]]
    corrections = client.get(
        "/api/v1/admin/feedback", params={"event_type": "knowledge_correction"}
    )
    assert {item["id"] for item in corrections.json()["items"]} == {ids[1], ids[3]}
    assert client.get("/api/v1/admin/feedback?cursor=bad").status_code == 400
    assert client.get("/api/v1/admin/feedback?limit=0").status_code == 422
    assert client.get("/api/v1/admin/feedback?limit=101").status_code == 422
    preview = next(
        item
        for item in client.get("/api/v1/admin/feedback").json()["items"]
        if item["id"] == ids[4]
    )
    assert preview["question_excerpt"] == "😀" * 280
    assert preview["question_length"] == 281

    detail = client.get(f"/api/v1/admin/feedback/{ids[4]}")
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    body = detail.json()
    assert body["agent_reported"] == {
        "question": "😀" * 281,
        "correction": None,
        "previous_answer": "Old answer",
        "context": "Context",
        "source_reference": "ref",
        "agent_id": "agent-7",
        "conversation_id": "thread-1",
        "source_event_id": "source-1",
        "user_reference": "user-1",
        "metadata": {"source": "test"},
    }
    assert body["tags"] == [
        {
            "slug": "it",
            "name": "IT",
            "from_default": True,
            "from_submission": True,
            "from_admin": False,
        }
    ]
    assert body["system"] == {
        "project": {"id": active.id, "name": "Active", "slug": "active"},
        "submitted_via": "rest",
        "api_token": {"id": token.id, "name": token.name, "prefix": "kebivo_pt_abc"},
        "idempotency_key": "key-1",
        "received_at": rows[4].created_at.isoformat(),
        "processing_status": "pending",
    }
    assert client.get(f"/api/v1/admin/feedback/{uuid4()}").status_code == 404
    app.state.engine.dispose()


def test_feedback_inbox_requires_session_and_uses_error_envelope(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "inbox-auth.db")
    for path in ("/api/v1/admin/feedback", f"/api/v1/admin/feedback/{uuid4()}"):
        bearer = client.get(path, headers={"Authorization": "Bearer project-token"})
        assert bearer.status_code == 401
        assert bearer.headers["cache-control"] == "no-store"
        assert bearer.json()["error"]["code"] == "unauthorized"
    client.cookies.clear()
    missing = client.get("/api/v1/admin/feedback")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["cache-control"] == "no-store"
    app.state.engine.dispose()
