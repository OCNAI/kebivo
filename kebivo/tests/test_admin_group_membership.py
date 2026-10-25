"""Membership changes preserve group and deletion invariants."""

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from httpx2 import Response
from sqlalchemy import func, select, text

from kebivo.feedback.groups import check_integrity
from kebivo.models import (
    AuditEvent,
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    IdempotencyRecord,
    ProcessingJob,
    Project,
    RoutingMatch,
)
from kebivo.tests.test_admin_groups import seeded
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_tokens import issue_token
from kebivo.worker.runner import Worker


def _integrity(app: object) -> None:
    with app.state.session_factory() as db:  # type: ignore[attr-defined]
        assert check_integrity(db) == []
        assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == db.scalar(
            select(func.sum(FeedbackGroup.occurrence_count))
        )


def test_merge_split_occurrences_and_provenance(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    headers = write_headers(client)
    target = ids[0][0]
    sources = [ids[2][0], ids[4][0]]
    assert (
        client.post(
            f"/api/v1/admin/groups/{target}/merge",
            json={"source_group_ids": [ids[6][0]]},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/admin/groups/{sources[0]}/merge",
            json={"source_group_ids": [ids[8][0]]},
            headers=headers,
        ).status_code
        == 200
    )
    with app.state.session_factory() as db:
        db.add(FeedbackGroupNote(group_id=sources[0], body="private note"))
        db.commit()
    response = client.post(
        f"/api/v1/admin/groups/{target}/merge", json={"source_group_ids": sources}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["occurrence_count"] == 5
    events = client.get(f"/api/v1/admin/groups/{target}/events").json()["items"]
    assert {row["id"] for row in events} == {ids[i][1] for i in (0, 2, 4, 6, 8)}
    assert {row["agent_reported"]["question"] for row in events} == {
        "alpha record 0",
        "alpha record 2",
        "alpha record 4",
        "alpha record 6",
        "alpha record 8",
    }
    with app.state.session_factory() as db:
        assert (
            db.scalar(select(FeedbackGroupNote).where(FeedbackGroupNote.group_id == target)).body
            == "private note"
        )
        moved = db.scalars(
            select(FeedbackGroupHistory).where(
                FeedbackGroupHistory.group_id == target,
                FeedbackGroupHistory.origin_group_id.in_(sources),
            )
        ).all()
        assert {row.origin_group_id for row in moved} == set(sources)
        assert {db.get(FeedbackGroupMember, ids[i][1]).match_method for i in (2, 4, 8)} == {
            "manual_merge"
        }
    split = client.post(
        f"/api/v1/admin/groups/{target}/split",
        json={"event_ids": [ids[2][1], ids[8][1]], "representative_event_id": ids[8][1]},
        headers=headers,
    )
    assert split.status_code == 201, split.text
    new_id = split.json()["group_id"]
    with app.state.session_factory() as db:
        assert db.get(FeedbackGroup, new_id).status == "open"
        assert db.get(FeedbackGroup, new_id).representative_event_id == ids[8][1]
        assert {db.get(FeedbackGroupMember, ids[i][1]).match_method for i in (2, 8)} == {
            "manual_split"
        }
    remaining = client.get(f"/api/v1/admin/groups/{target}/events").json()["items"]
    separated = client.get(f"/api/v1/admin/groups/{new_id}/events").json()["items"]
    assert {row["id"] for row in remaining} == {ids[i][1] for i in (0, 4, 6)}
    assert {row["id"] for row in separated} == {ids[i][1] for i in (2, 8)}
    _integrity(app)


def test_merge_incompatible_and_split_all_are_unchanged(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    headers = write_headers(client)
    target = ids[0][0]
    before = client.get(f"/api/v1/admin/groups/{target}").json()
    wrong_type = client.post(
        f"/api/v1/admin/groups/{target}/merge",
        json={"source_group_ids": [ids[1][0]]},
        headers=headers,
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "merge_incompatible"
    with app.state.session_factory() as db:
        foreign_project = Project(name="Other", slug="other")
        db.add(foreign_project)
        db.flush()
        other = FeedbackEvent(
            project_id=foreign_project.id,
            submitted_via="rest",
            event_type="knowledge_gap",
            question="another project",
            normalized_question_hash="a" * 64,
        )
        db.add(other)
        db.flush()
        from kebivo.feedback.groups import create_initial_group

        foreign_group = create_initial_group(db, other)
        db.commit()
        foreign_id = foreign_group.id
    cross = client.post(
        f"/api/v1/admin/groups/{target}/merge",
        json={"source_group_ids": [foreign_id]},
        headers=headers,
    )
    assert cross.status_code == 422
    assert cross.json()["error"]["code"] == "merge_incompatible"
    all_members = client.post(
        f"/api/v1/admin/groups/{target}/split", json={"event_ids": [ids[0][1]]}, headers=headers
    )
    assert all_members.status_code == 422
    assert all_members.json()["error"]["code"] == "split_all_members"
    assert client.get(f"/api/v1/admin/groups/{target}").json() == before
    _integrity(app)


def test_merge_rejects_more_than_500_notes(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    target, source = ids[0][0], ids[2][0]
    with app.state.session_factory() as db:
        db.add_all(
            [FeedbackGroupNote(group_id=target, body="target") for _ in range(250)]
            + [FeedbackGroupNote(group_id=source, body="source") for _ in range(251)]
        )
        db.commit()
    response = client.post(
        f"/api/v1/admin/groups/{target}/merge",
        json={"source_group_ids": [source]},
        headers=write_headers(client),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "note_limit"
    with app.state.session_factory() as db:
        assert db.get(FeedbackGroup, source) is not None
        assert db.scalar(select(func.count()).select_from(FeedbackGroupNote)) == 501
    _integrity(app)


def test_note_add_racing_merge_returns_valid_response(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    target, source = ids[0][0], ids[2][0]
    headers = write_headers(client)
    barrier = threading.Barrier(2)

    def add_note() -> Response:
        barrier.wait()
        return client.post(
            f"/api/v1/admin/groups/{source}/notes", json={"body": "race note"}, headers=headers
        )

    def merge() -> Response:
        barrier.wait()
        return client.post(
            f"/api/v1/admin/groups/{target}/merge",
            json={"source_group_ids": [source]},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        note_future = pool.submit(add_note)
        merge_future = pool.submit(merge)
        note_response = note_future.result()
        merge_response = merge_future.result()
    assert merge_response.status_code == 200, merge_response.text
    assert note_response.status_code in (201, 404), note_response.text
    with app.state.session_factory() as db:
        notes = db.scalars(select(FeedbackGroupNote)).all()
        assert len(notes) == (1 if note_response.status_code == 201 else 0)
        assert all(note.group_id == target for note in notes)
    _integrity(app)


def test_seeded_sequence_keeps_counts_and_membership(tmp_path: Path) -> None:
    client, app, project, _ = seeded(tmp_path)
    headers = write_headers(client)
    token = issue_token(client, project["id"], "sequence").json()["token"]
    rng = random.Random(103)
    for step in range(30):
        with app.state.session_factory() as db:
            groups = db.scalars(select(FeedbackGroup).order_by(FeedbackGroup.id)).all()
            group_members = {
                group.id: db.scalars(
                    select(FeedbackGroupMember).where(FeedbackGroupMember.group_id == group.id)
                ).all()
                for group in groups
            }
            gaps = [group.id for group in groups if group.event_type == "knowledge_gap"]
            splittable = [group.id for group in groups if len(group_members[group.id]) > 1]
            events = db.scalars(select(FeedbackEvent).order_by(FeedbackEvent.id)).all()
            event_ids = [event.id for event in events]
        operation = ("ingest", "merge", "split", "delete", "status")[step % 5]
        if operation == "ingest":
            response = client.post(
                "/api/v1/feedback",
                json={
                    "event_type": "knowledge_gap",
                    "question": f"sequence canary {step}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 202, response.text
        elif operation == "merge" and len(gaps) > 1:
            target, source = rng.sample(gaps, 2)
            response = client.post(
                f"/api/v1/admin/groups/{target}/merge",
                json={"source_group_ids": [source]},
                headers=headers,
            )
            assert response.status_code == 200, response.text
        elif operation == "split" and splittable:
            source = rng.choice(splittable)
            event_id = rng.choice(group_members[source]).event_id
            response = client.post(
                f"/api/v1/admin/groups/{source}/split",
                json={"event_ids": [event_id]},
                headers=headers,
            )
            assert response.status_code == 201, response.text
        elif operation == "delete" and event_ids:
            response = client.delete(
                f"/api/v1/admin/feedback/{rng.choice(event_ids)}", headers=headers
            )
            assert response.status_code == 200, response.text
        elif operation == "status" and groups:
            source = rng.choice(groups).id
            response = client.patch(
                f"/api/v1/admin/groups/{source}", json={"status": "resolved"}, headers=headers
            )
            assert response.status_code == 200, response.text
        _integrity(app)


@pytest.mark.parametrize("operation", ["merge", "split"])
def test_fault_rolls_back_every_membership_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    client, app, _, ids = seeded(tmp_path)
    headers = write_headers(client)
    if operation == "split":
        assert (
            client.post(
                f"/api/v1/admin/groups/{ids[0][0]}/merge",
                json={"source_group_ids": [ids[2][0]]},
                headers=headers,
            ).status_code
            == 200
        )
    import kebivo.admin_group_membership as membership

    original = membership._recompute

    def fail_after_write(*args: object) -> None:
        original(*args)  # type: ignore[arg-type]
        raise RuntimeError("injected fault")

    monkeypatch.setattr(membership, "_recompute", fail_after_write)
    with pytest.raises(RuntimeError, match="injected fault"):
        if operation == "merge":
            client.post(
                f"/api/v1/admin/groups/{ids[0][0]}/merge",
                json={"source_group_ids": [ids[2][0]]},
                headers=headers,
            )
        else:
            client.post(
                f"/api/v1/admin/groups/{ids[0][0]}/split",
                json={"event_ids": [ids[0][1]]},
                headers=headers,
            )
    with app.state.session_factory() as db:
        assert db.get(FeedbackGroupMember, ids[2][1]).group_id == (
            ids[2][0] if operation == "merge" else ids[0][0]
        )
        assert db.get(FeedbackGroupMember, ids[0][1]).group_id == ids[0][0]
    _integrity(app)


def test_delete_cascades_and_busy_delivery(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    headers = write_headers(client)
    event_id = ids[0][1]
    group_id = ids[0][0]
    with app.state.session_factory() as db:
        destination = Destination(name="canary", type="inbox")
        db.add(destination)
        db.flush()
        delivery = Delivery(
            event_id=event_id,
            project_id=project["id"],
            destination_id=destination.id,
            status="processing",
        )
        db.add(delivery)
        db.flush()
        db.add(DeliveryAttempt(delivery_id=delivery.id, attempt_number=1, trigger="auto"))
        db.add(FeedbackGroupNote(group_id=group_id, body="delete canary"))
        db.commit()
        delivery_id = delivery.id
    busy = client.delete(f"/api/v1/admin/feedback/{event_id}", headers=headers)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "feedback_busy"
    with app.state.session_factory() as db:
        db.get(Delivery, delivery_id).status = "pending"
        db.commit()
    deleted = client.delete(f"/api/v1/admin/feedback/{event_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"group_id": group_id, "group_deleted": True}
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, event_id) is None
        assert db.get(FeedbackGroup, group_id) is None
        assert db.get(Delivery, delivery_id) is None
        assert db.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(FeedbackEventTag)
                .where(FeedbackEventTag.event_id == event_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProcessingJob)
                .where(ProcessingJob.event_id == event_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(RoutingMatch)
                .where(RoutingMatch.event_id == event_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.event_id == event_id)
            )
            == 0
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM feedback_search WHERE feedback_search MATCH 'alpha' "
                    "AND event_id=:id"
                ),
                {"id": event_id},
            ).scalar_one()
            == 0
        )
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "feedback.deleted"))
        assert audit is not None
        assert "alpha" not in audit.details_json and "delete canary" not in audit.details_json
        assert json.loads(audit.details_json) == {
            "project_id": project["id"],
            "group_id": group_id,
            "event_type": "knowledge_gap",
            "group_deleted": True,
            "deliveries_removed_by_status": {"pending": 1},
        }
    _integrity(app)


def test_delete_removes_feedback_text_from_database_and_wal(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    event_id = ids[0][1]
    marker = "DELETED_FEEDBACK_CANARY_9e4f8f3b"
    with app.state.session_factory() as db:
        db.get(FeedbackEvent, event_id).question = marker
        db.commit()
    path = tmp_path / "feedback.db"
    assert any(
        marker.encode() in candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"))
        if candidate.exists()
    )
    response = client.delete(
        f"/api/v1/admin/feedback/{event_id}", headers=write_headers(client)
    )
    assert response.status_code == 200, response.text
    assert marker.encode() not in path.read_bytes()
    wal = Path(f"{path}-wal")
    if wal.exists():
        assert marker.encode() not in wal.read_bytes()
    app.state.engine.dispose()


def test_delete_reassigns_representative_and_rejects_active_job(tmp_path: Path) -> None:
    client, app, project, ids = seeded(tmp_path)
    headers = write_headers(client)
    target = ids[0][0]
    assert (
        client.post(
            f"/api/v1/admin/groups/{target}/merge",
            json={"source_group_ids": [ids[2][0]]},
            headers=headers,
        ).status_code
        == 200
    )
    with app.state.session_factory() as db:
        db.add(ProcessingJob(event_id=ids[0][1], status="processing"))
        db.commit()
    busy = client.delete(f"/api/v1/admin/feedback/{ids[0][1]}", headers=headers)
    assert busy.status_code == 409
    with app.state.session_factory() as db:
        db.scalar(
            select(ProcessingJob).where(ProcessingJob.event_id == ids[0][1])
        ).status = "pending"
        db.commit()
    deleted = client.delete(f"/api/v1/admin/feedback/{ids[0][1]}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["group_deleted"] is False
    with app.state.session_factory() as db:
        group = db.get(FeedbackGroup, target)
        assert group.representative_event_id == ids[2][1]
        assert group.occurrence_count == 1
        actions = {
            row.action
            for row in db.scalars(
                select(FeedbackGroupHistory).where(FeedbackGroupHistory.group_id == target)
            )
        }
        assert {"event_deleted", "representative_changed"}.issubset(actions)
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "feedback.deleted", AuditEvent.subject_id == ids[0][1]
            )
        )
        assert json.loads(audit.details_json)["project_id"] == project["id"]
    _integrity(app)


def test_delete_racing_worker_claim_has_no_orphan(tmp_path: Path) -> None:
    client, app, project, _ = seeded(tmp_path)
    token = issue_token(client, project["id"], "race").json()["token"]
    submitted = client.post(
        "/api/v1/feedback",
        json={"event_type": "knowledge_gap", "question": "race canary"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert submitted.status_code == 202
    event_id = submitted.json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    barrier = threading.Barrier(2)

    def claim() -> list[str]:
        barrier.wait()
        return worker._claim("job", 1)

    def delete() -> Response:
        barrier.wait()
        return client.delete(f"/api/v1/admin/feedback/{event_id}", headers=write_headers(client))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim)
        delete_future = pool.submit(delete)
        claimed = claim_future.result()
        response = delete_future.result()
    assert response.status_code in (200, 409), response.text
    with app.state.session_factory() as db:
        event = db.get(FeedbackEvent, event_id)
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        if response.status_code == 200:
            assert event is None and job is None and claimed == []
        else:
            assert event is not None and job is not None and job.status == "processing"
    _integrity(app)


def test_new_writes_require_admin_and_csrf(tmp_path: Path) -> None:
    client, app, _, ids = seeded(tmp_path)
    paths = [
        ("post", f"/api/v1/admin/groups/{ids[0][0]}/merge", {"source_group_ids": [ids[2][0]]}),
        ("post", f"/api/v1/admin/groups/{ids[0][0]}/split", {"event_ids": [ids[0][1]]}),
        ("delete", f"/api/v1/admin/feedback/{ids[0][1]}", None),
    ]
    for method, path, body in paths:
        call = getattr(client, method)
        kwargs = {"json": body} if body is not None else {}
        assert (
            call(path, headers={"Origin": "https://admin.example.test"}, **kwargs).status_code
            == 403
        )
        bearer = call(path, headers={"Authorization": "Bearer project-token"}, **kwargs)
        assert bearer.status_code == 401
        assert bearer.headers["cache-control"] == "no-store"
    _integrity(app)
