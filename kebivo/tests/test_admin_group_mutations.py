"""Group triage writes and their security invariants."""

import json
import logging
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kebivo import admin_group_mutations
from kebivo.app import create_app
from kebivo.feedback.groups import check_integrity
from kebivo.models import (
    AuditEvent,
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    Tag,
)
from kebivo.settings import Settings
from kebivo.tests.test_admin_groups import seeded
from kebivo.tests.test_projects import write_headers


def _counts(app: object) -> tuple[int, int]:
    with app.state.session_factory() as db:  # type: ignore[attr-defined]
        return (
            db.scalar(select(func.count()).select_from(Delivery)) or 0,
            db.scalar(select(func.count()).select_from(DeliveryAttempt)) or 0,
        )


def test_auth_and_csrf_on_every_group_mutation(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    group_id = ids[0][0]
    note_id = "00000000-0000-0000-0000-000000000000"
    actions = (
        ("patch", f"/api/v1/admin/groups/{group_id}", {"status": "resolved"}),
        ("post", f"/api/v1/admin/groups/{group_id}/notes", {"body": "hi"}),
        ("delete", f"/api/v1/admin/groups/{group_id}/notes/{note_id}", None),
        ("patch", f"/api/v1/admin/groups/{group_id}/tags", {"add": ["ops"]}),
    )
    for method, path, body in actions:
        call = getattr(client, method)
        kwargs = {"json": body} if body is not None else {}
        assert (
            call(path, headers={"Origin": "https://admin.example.test"}, **kwargs).status_code
            == 403
        )
        bearer = call(path, headers={"Authorization": "Bearer project-token"}, **kwargs)
        assert bearer.status_code == 401
        assert bearer.headers["cache-control"] == "no-store"
    app.state.engine.dispose()


def test_status_representative_and_ac13_persist(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    path = "/api/v1/admin/groups"
    with app.state.session_factory() as db:
        # Keep precisely two open IT groups and one resolved IT group.
        db.get(FeedbackGroup, ids[8][0]).status = "resolved"
        db.get(FeedbackGroup, ids[2][0]).status = "in_progress"
        db.get(FeedbackGroup, ids[6][0]).status = "in_progress"
        db.commit()

    def filtered(status: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            client.get(path, params={"status": status, "tag": "it"}).json()["items"],
        )

    assert len(filtered("open")) == 2
    group_id, event_id, _ = ids[0]
    headers = write_headers(client)
    changed = client.patch(f"{path}/{group_id}", json={"status": "resolved"}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["resolved_at"]
    assert len(filtered("open")) == 1
    assert {row["id"] for row in filtered("resolved")} == {group_id, ids[8][0]}
    with app.state.session_factory() as db:
        count = db.scalar(
            select(func.count())
            .select_from(FeedbackGroupHistory)
            .where(
                FeedbackGroupHistory.group_id == group_id,
                FeedbackGroupHistory.action == "status_changed",
            )
        )
    repeated = client.patch(f"{path}/{group_id}", json={"status": "resolved"}, headers=headers)
    assert repeated.status_code == 200
    with app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(FeedbackGroupHistory)
                .where(
                    FeedbackGroupHistory.group_id == group_id,
                    FeedbackGroupHistory.action == "status_changed",
                )
            )
            == count
        )
        second = FeedbackGroupMember(
            event_id=ids[2][1], group_id=group_id, match_method="manual_merge"
        )
        db.delete(db.get(FeedbackGroupMember, ids[2][1]))
        db.flush()
        db.add(second)
        db.delete(db.get(FeedbackGroup, ids[2][0]))
        group = db.get(FeedbackGroup, group_id)
        group.occurrence_count = 2
        group.last_seen_at = db.get(FeedbackGroup, ids[2][0]).last_seen_at
        db.commit()
    invalid = client.patch(
        f"{path}/{group_id}", json={"representative_event_id": ids[4][1]}, headers=headers
    )
    assert invalid.status_code == 422
    rep = client.patch(
        f"{path}/{group_id}", json={"representative_event_id": ids[2][1]}, headers=headers
    )
    assert rep.status_code == 200 and rep.json()["representative_event_id"] == ids[2][1]
    reopened = client.patch(f"{path}/{group_id}", json={"status": "open"}, headers=headers)
    assert reopened.status_code == 200 and reopened.json()["resolved_at"] is None
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.subject_id == group_id,
                    AuditEvent.action == "feedback_group.status_changed",
                )
            )
            == 2
        )
    app.state.engine.dispose()
    restarted = create_app(
        Settings(
            "Kebivo",
            "test",
            "project-tests-secret-key-000000000000",
            str(tmp_path / "feedback.db"),
            allowed_origins=("https://admin.example.test",),
        )
    )
    fresh = TestClient(restarted, base_url="https://admin.example.test")
    login = fresh.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": "https://admin.example.test"},
    )
    assert login.status_code == 200
    assert {
        row["id"] for row in fresh.get(path, params={"status": "open", "tag": "it"}).json()["items"]
    } == {group_id, ids[4][0]}
    restarted.state.engine.dispose()


def test_representative_uuid_forms_are_canonical(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    headers = write_headers(client)
    group_id, first_id, _ = ids[0]
    second_id = ids[2][1]
    assert client.post(
        f"/api/v1/admin/groups/{group_id}/merge",
        json={"source_group_ids": [ids[2][0]]},
        headers=headers,
    ).status_code == 200
    event_uuid = UUID(second_id)
    variants = (second_id.upper(), event_uuid.hex, f"{{{second_id}}}", event_uuid.urn)
    for variant in variants:
        response = client.patch(
            f"/api/v1/admin/groups/{group_id}",
            json={"representative_event_id": variant},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["representative_event_id"] == second_id
        with app.state.session_factory() as db:
            trail = db.scalars(
                select(FeedbackGroupHistory).where(
                    FeedbackGroupHistory.group_id == group_id,
                    FeedbackGroupHistory.action == "representative_changed",
                ).order_by(FeedbackGroupHistory.created_at.desc())
            ).first()
            assert json.loads(trail.details_json)["to_event_id"] == second_id
        assert client.patch(
            f"/api/v1/admin/groups/{group_id}",
            json={"representative_event_id": first_id},
            headers=headers,
        ).status_code == 200
    invalid = client.patch(
        f"/api/v1/admin/groups/{group_id}",
        json={"representative_event_id": "not-a-uuid"},
        headers=headers,
    )
    assert invalid.status_code == 422
    app.state.engine.dispose()


def test_notes_tags_trails_and_deliveries(tmp_path: Path, caplog: object) -> None:
    client, app, project, ids = seeded(tmp_path)
    group_id, event_id, _ = ids[0]
    headers = write_headers(client)
    base = f"/api/v1/admin/groups/{group_id}"
    with app.state.session_factory() as db:
        destination = Destination(name="Existing delivery", type="inbox")
        db.add(destination)
        db.flush()
        db.add(
            Delivery(
                event_id=event_id,
                project_id=project["id"],
                destination_id=destination.id,
                status="delivered",
            )
        )
        db.commit()
    before = _counts(app)
    canary = "NOTE_CANARY_6caa <script>alert(1)</script>"
    with caplog.at_level(logging.INFO):  # type: ignore[attr-defined]
        note = client.post(f"{base}/notes", json={"body": canary}, headers=headers)
        assert note.status_code == 201, note.text
        assert note.json()["body"] == canary
        added = client.patch(f"{base}/tags", json={"add": ["New Ops"]}, headers=headers)
        assert added.status_code == 200, added.text
        removed = client.patch(f"{base}/tags", json={"remove": ["IT"]}, headers=headers)
        assert removed.status_code == 200, removed.text
        deleted = client.delete(f"{base}/notes/{note.json()['id']}", headers=headers)
        assert deleted.status_code == 200
    assert _counts(app) == before
    with app.state.session_factory() as db:
        assert db.get(FeedbackGroupNote, note.json()["id"]) is None
        history = db.scalars(
            select(FeedbackGroupHistory).where(FeedbackGroupHistory.group_id == group_id)
        ).all()
        audits = db.scalars(select(AuditEvent).where(AuditEvent.subject_id == group_id)).all()
        assert [row.action for row in history if row.actor_type == "admin"] == [
            "note_added",
            "tags_changed",
            "tags_changed",
            "note_deleted",
        ]
        assert len(audits) == 4
        assert canary not in "".join(row.details_json for row in history + audits)
        assert (
            "it"
            in json.loads(
                next(
                    row.details_json
                    for row in history
                    if row.action == "tags_changed"
                    and '"removed"' in row.details_json
                    and '"it"' in row.details_json
                )
            )["removed"]
        )
        tag = db.scalar(select(Tag).where(Tag.slug == "new-ops", Tag.project_id == project["id"]))
        assert tag is not None
        link = db.get(FeedbackEventTag, (event_id, tag.id))
        assert link is not None and link.from_admin
        assert (
            db.get(
                FeedbackEventTag,
                (
                    event_id,
                    db.scalar(
                        select(Tag.id).where(Tag.slug == "it", Tag.project_id == project["id"])
                    ),
                ),
            )
            is None
        )
    assert canary not in caplog.text  # type: ignore[attr-defined]
    app.state.engine.dispose()


def test_tag_project_scope_and_event_limit(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    from kebivo.tests.test_tokens import create_project

    other = create_project(client, "other")
    group_id, event_id, _ = ids[0]
    with app.state.session_factory() as db:
        foreign = Tag(project_id=other["id"], name="Foreign", slug="shared")
        db.add(foreign)
        db.flush()
        for index in range(28):
            tag = Tag(project_id=project["id"], name=f"Fill {index}", slug=f"fill-{index}")
            db.add(tag)
            db.flush()
            db.add(FeedbackEventTag(event_id=event_id, tag_id=tag.id, from_admin=True))
        db.commit()
    headers = write_headers(client)
    path = f"/api/v1/admin/groups/{group_id}/tags"
    denied = client.patch(path, json={"add": ["shared"]}, headers=headers)
    assert denied.status_code == 422
    with app.state.session_factory() as db:
        assert (
            db.scalar(select(Tag).where(Tag.project_id == project["id"], Tag.slug == "shared"))
            is None
        )
    accepted = client.patch(path, json={"add": ["shared"], "remove": ["it"]}, headers=headers)
    assert accepted.status_code == 200, accepted.text
    with app.state.session_factory() as db:
        local = db.scalar(select(Tag).where(Tag.project_id == project["id"], Tag.slug == "shared"))
        foreign = db.scalar(select(Tag).where(Tag.project_id == other["id"], Tag.slug == "shared"))
        assert local is not None and foreign is not None and local.id != foreign.id
        assert db.get(FeedbackEventTag, (event_id, local.id)) is not None
        assert db.get(FeedbackEventTag, (event_id, foreign.id)) is None
    app.state.engine.dispose()


def test_tag_edit_member_cap_and_joined_tags(tmp_path: Path, monkeypatch: Any) -> None:
    client, app, _, ids = seeded(tmp_path)
    headers = write_headers(client)
    group_id = ids[0][0]
    merge = client.post(
        f"/api/v1/admin/groups/{group_id}/merge",
        json={"source_group_ids": [ids[2][0]]},
        headers=headers,
    )
    assert merge.status_code == 200, merge.text
    path = f"/api/v1/admin/groups/{group_id}/tags"
    monkeypatch.setattr(admin_group_mutations, "MAX_TAG_EDIT_MEMBERS", 1)
    capped = client.patch(path, json={"add": ["ops"]}, headers=headers)
    assert capped.status_code == 422
    assert capped.json()["error"]["code"] == "tag_edit_limit"
    monkeypatch.setattr(admin_group_mutations, "MAX_TAG_EDIT_MEMBERS", 10_000)
    edited = client.patch(path, json={"add": ["ops"]}, headers=headers)
    assert edited.status_code == 200, edited.text
    with app.state.session_factory() as db:
        tag = db.scalar(select(Tag).where(Tag.slug == "ops"))
        assert tag is not None
        assert all(
            db.get(FeedbackEventTag, (event_id, tag.id))
            for event_id in (ids[0][1], ids[2][1])
        )
    app.state.engine.dispose()
