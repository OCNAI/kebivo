"""Retention and export invariants for the Phase 7 kebivo."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kebivo.admin_group_membership import delete_feedback_event
from kebivo.cli import main as cli_main
from kebivo.feedback.groups import check_integrity, create_initial_group
from kebivo.models import (
    AuditEvent,
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupMember,
    MaintenanceRun,
    ProcessingJob,
)
from kebivo.retention import due, preview, run
from kebivo.settings import Settings, SettingsError
from kebivo.tests.test_admin_groups import seeded
from kebivo.tests.test_projects import ORIGIN, make_client, write_headers
from kebivo.tests.test_tokens import issue_token


def setup(tmp_path: Path) -> tuple[TestClient, FastAPI, dict[str, str], dict[str, str]]:
    client, app = make_client(tmp_path / "retention.db")
    a = client.post(
        "/api/v1/projects", json={"name": "A", "slug": "a"}, headers=write_headers(client)
    ).json()
    b = client.post(
        "/api/v1/projects", json={"name": "B", "slug": "b"}, headers=write_headers(client)
    ).json()
    return client, app, a, b


def seed(app: FastAPI, project_id: str, created_at: datetime, question: str = "canary") -> str:
    with app.state.session_factory() as db:
        event = FeedbackEvent(
            id=str(uuid4()),
            project_id=project_id,
            submitted_via="rest",
            event_type="knowledge_gap",
            question=question,
            normalized_question_hash=hashlib.sha256(question.encode()).hexdigest(),
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(event)
        db.flush()
        create_initial_group(db, event)
        db.commit()
        return event.id


def test_retention_matches_manual_group_delete_and_integrity(tmp_path: Path) -> None:
    client, app, a, _ = setup(tmp_path)
    old = datetime(2025, 1, 1, tzinfo=UTC)
    manual = seed(app, a["id"], old, "manual")
    automatic = seed(app, a["id"], old, "automatic")
    with app.state.session_factory() as db:
        manual_member = db.get(FeedbackGroupMember, manual)
        automatic_member = db.get(FeedbackGroupMember, automatic)
        assert manual_member is not None and automatic_member is not None
        target_id, source_id = manual_member.group_id, automatic_member.group_id
    assert (
        client.post(
            f"/api/v1/admin/groups/{target_id}/merge",
            json={"source_group_ids": [source_id]},
            headers=write_headers(client),
        ).status_code
        == 200
    )
    assert (
        client.delete(f"/api/v1/admin/feedback/{manual}", headers=write_headers(client)).status_code
        == 200
    )
    with app.state.session_factory() as db:
        group = db.get(FeedbackGroup, target_id)
        assert group is not None
        assert group.occurrence_count == 1 and group.representative_event_id == automatic
    counts = run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))
    assert counts["feedback"] == 1
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, automatic) is None
        assert db.get(FeedbackEvent, manual) is None
        assert db.scalar(select(func.count()).select_from(FeedbackGroup)) == 0
        assert check_integrity(db) == []
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "retention.completed")
            )
            == 1
        )


def test_retention_removes_fts_rows_on_migrated_database(tmp_path: Path) -> None:
    _, app, _, ids = seeded(tmp_path)
    deleted_id = ids[0][1]
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2027, 1, 1, tzinfo=UTC))[
            "feedback"
        ]
        == 9
    )
    with app.state.session_factory() as db:
        assert db.execute(text("SELECT count(*) FROM feedback_search")).scalar_one() == 0
        assert db.get(FeedbackEvent, deleted_id) is None
        assert check_integrity(db) == []


def test_busy_event_skipped_then_removed(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    event_id = seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC))
    with app.state.session_factory() as db:
        db.add(ProcessingJob(event_id=event_id, status="processing"))
        db.commit()
    first = run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))
    assert first["skipped_busy"] == 1 and first["feedback"] == 0
    with app.state.session_factory() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        assert job is not None
        job.status = "completed"
        db.commit()
    second = run(app.state.session_factory, app.state.settings, datetime(2026, 1, 2, tzinfo=UTC))
    assert second["feedback"] == 1


def test_processing_delivery_is_skipped(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    event_id = seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC))
    with app.state.session_factory() as db:
        destination = Destination(name="Busy inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        delivery = Delivery(
            event_id=event_id,
            project_id=a["id"],
            destination_id=destination.id,
            status="processing",
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))[
            "skipped_busy"
        ]
        == 1
    )
    with app.state.session_factory() as db:
        delivery = db.get(Delivery, delivery_id)
        assert delivery is not None
        delivery.status = "failed"
        db.commit()
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 2, tzinfo=UTC))[
            "feedback"
        ]
        == 1
    )


def test_boundaries_and_zero_are_independent(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    cutoff = clock - timedelta(days=180)
    older = seed(app, a["id"], cutoff - timedelta(seconds=1), "older")
    exact = seed(app, a["id"], cutoff, "exact")
    settings = replace(app.state.settings, feedback_retention_days=0)
    assert preview(app.state.session_factory, settings, clock)["feedback"] == 0
    run(app.state.session_factory, settings, clock)
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, older) is not None
    assert run(app.state.session_factory, app.state.settings, clock)["feedback"] == 1
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, older) is None
        assert db.get(FeedbackEvent, exact) is not None


def test_each_retention_class_uses_its_own_setting(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    old = datetime(2025, 1, 1, tzinfo=UTC)
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    event_id = seed(app, a["id"], old, "independent")
    with app.state.session_factory() as db:
        destination = Destination(name="Retention inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        delivery = Delivery(
            event_id=event_id,
            project_id=a["id"],
            destination_id=destination.id,
            status="failed",
            attempt_count=1,
        )
        db.add(delivery)
        db.flush()
        attempt = DeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=1,
            trigger="auto",
            started_at=old,
            completed_at=old,
            result="permanent_failure",
        )
        audit = AuditEvent(
            actor_type="system",
            action="test.old",
            subject_type="test",
            created_at=old,
        )
        db.add_all([attempt, audit])
        db.commit()
        attempt_id, audit_id = attempt.id, audit.id
    disabled = replace(
        app.state.settings,
        feedback_retention_days=0,
        delivery_log_retention_days=0,
        audit_retention_days=0,
    )
    assert all(
        run(app.state.session_factory, disabled, clock)[key] == 0
        for key in ("feedback", "delivery_attempts", "audit_events")
    )
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, event_id) is not None
        assert db.get(DeliveryAttempt, attempt_id) is not None
        assert db.get(AuditEvent, audit_id) is not None
    delivery_only = replace(disabled, delivery_log_retention_days=30)
    assert run(app.state.session_factory, delivery_only, clock)["delivery_attempts"] == 1
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, event_id) is not None
        assert db.get(AuditEvent, audit_id) is not None
    audit_only = replace(disabled, audit_retention_days=90)
    assert run(app.state.session_factory, audit_only, clock)["audit_events"] == 1
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, event_id) is not None
    feedback_only = replace(disabled, feedback_retention_days=180)
    assert run(app.state.session_factory, feedback_only, clock)["feedback"] == 1


def test_attempt_pruning_preserves_delivery_state_and_number(tmp_path: Path) -> None:
    client, app, a, _ = setup(tmp_path)
    old = datetime(2025, 1, 1, tzinfo=UTC)
    event_id = seed(app, a["id"], old)
    with app.state.session_factory() as db:
        destination = Destination(name="Inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        delivery = Delivery(
            event_id=event_id,
            project_id=a["id"],
            destination_id=destination.id,
            status="failed",
            attempt_count=2,
            max_attempts=5,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                attempt_number=2,
                trigger="auto",
                started_at=old,
                completed_at=old,
                result="permanent_failure",
            )
        )
        delivery_id = delivery.id
        db.commit()
    settings = replace(app.state.settings, feedback_retention_days=0)
    assert (
        run(app.state.session_factory, settings, datetime(2026, 1, 1, tzinfo=UTC))[
            "delivery_attempts"
        ]
        == 1
    )
    with app.state.session_factory() as db:
        delivery = db.get(Delivery, delivery_id)
        assert delivery and delivery.status == "failed" and delivery.attempt_count == 2
    detail = client.get(f"/api/v1/deliveries/{delivery_id}").json()
    assert detail["attempt_details_removed_by_retention"] is True
    retried = client.post(f"/api/v1/deliveries/{delivery_id}/retry", headers=write_headers(client))
    assert retried.status_code == 200, retried.text
    assert retried.json()["attempt_count"] == 3
    assert [attempt["attempt_number"] for attempt in retried.json()["attempts"]] == [3]


def test_dry_run_and_settings_disclose_no_secret(tmp_path: Path) -> None:
    client, app, a, _ = setup(tmp_path)
    seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC))
    db_path = tmp_path / "retention.db"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert preview(app.state.session_factory, app.state.settings)["feedback"] == 1
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert client.get("/api/v1/admin/settings").status_code == 200
    payload = client.get("/api/v1/admin/settings").json()
    assert payload["version"] == "0.2.0"
    assert payload["retention"]["feedback_days"] == 180
    assert "secret" not in json.dumps(payload).lower()
    assert "version" not in client.get("/health/live").text
    assert (
        client.get(
            "/api/v1/admin/settings", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )


def test_export_scoped_and_admin_only(tmp_path: Path) -> None:
    client, app, a, b = setup(tmp_path)
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    a_id = seed(app, a["id"], clock, "same question")
    b_id = seed(app, b["id"], clock, "same question")
    other_a_ids = {seed(app, a["id"], clock, f"page-{index}") for index in range(101)}
    response = client.get(f"/api/v1/admin/projects/{a['id']}/export")
    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert {line["id"] for line in lines} == {a_id, *other_a_ids}
    assert len(lines) == 102
    schema_text = Path("docs/export.md").read_text().split("```json\n", 1)[1].split("\n```", 1)[0]
    schema = json.loads(schema_text)
    for line in lines:
        assert set(line) == set(schema["required"]) == set(schema["properties"])
        assert isinstance(line["tags"], list) and isinstance(line["deliveries"], list)
        assert line["group"] is not None
        assert set(line["group"]) == set(schema["properties"]["group"]["required"])
    assert b_id not in response.text
    assert "secret" not in response.text.lower()
    token = issue_token(client, a["id"], "export probe").json()["token"]
    assert (
        client.get(
            f"/api/v1/admin/projects/{a['id']}/export", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )
    assert (
        TestClient(app, base_url=ORIGIN).get(f"/api/v1/admin/projects/{a['id']}/export").status_code
        == 401
    )
    with app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "project.exported")
            )
            == 1
        )


def test_invalid_retention_settings_name_only() -> None:
    for name in ("FEEDBACK_RETENTION_DAYS", "DELIVERY_LOG_RETENTION_DAYS", "AUDIT_RETENTION_DAYS"):
        with pytest.raises(SettingsError, match=name) as caught:
            Settings.from_env({name: "private-invalid-value"})
        assert "private-invalid-value" not in str(caught.value)


def test_completed_run_suppresses_restart_until_next_day(tmp_path: Path) -> None:
    _, app, _, _ = setup(tmp_path)
    moment = datetime.now(UTC)
    with app.state.session_factory() as db:
        assert due(db, moment)
        db.add(
            MaintenanceRun(
                kind="retention",
                started_at=moment,
                finished_at=moment,
                status="completed",
                cutoffs_json="{}",
                counts_json="{}",
            )
        )
        db.commit()
    with app.state.session_factory() as db:
        assert not due(db, moment + timedelta(hours=23))
        assert due(db, moment + timedelta(hours=24, seconds=1))


def test_retention_keeps_last_thirty_runs(tmp_path: Path) -> None:
    _, app, _, _ = setup(tmp_path)
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    settings = replace(
        app.state.settings,
        feedback_retention_days=0,
        delivery_log_retention_days=0,
        audit_retention_days=0,
    )
    for day in range(31):
        run(app.state.session_factory, settings, clock + timedelta(days=day))
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(MaintenanceRun)) == 30


def test_deleted_content_absent_from_database_and_wal(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    canary = "RETENTION-CANARY-4e88c3a0"
    seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC), canary)
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))[
            "feedback"
        ]
        == 1
    )
    for suffix in ("", "-wal"):
        path = tmp_path / f"retention.db{suffix}"
        if path.exists():
            assert canary.encode() not in path.read_bytes()


def test_retention_trails_contain_counts_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, app, a, _ = setup(tmp_path)
    canary = "RETENTION-TRAIL-CANARY-82c99"
    seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC), canary)
    run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))
    with app.state.session_factory() as db:
        row = db.scalar(select(MaintenanceRun).order_by(MaintenanceRun.started_at.desc()))
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "retention.completed"))
        assert row is not None and audit is not None
        for trail in (row.cutoffs_json, row.counts_json, audit.details_json, caplog.text):
            assert canary not in trail
        assert set(json.loads(row.counts_json)) >= {"feedback", "skipped_busy"}


def test_fault_between_batches_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, app, a, _ = setup(tmp_path)
    for index in range(202):
        seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC), f"fault-{index}")
    import kebivo.retention as retention

    original = delete_feedback_event
    calls = 0

    def fail_once(db: Session, event: FeedbackEvent, actor: str) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        if calls == 201:
            raise RuntimeError("injected fault")
        return original(db, event, actor)

    monkeypatch.setattr(retention, "delete_feedback_event", fail_once)
    with pytest.raises(RuntimeError, match="injected fault"):
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
        assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == 2
    monkeypatch.setattr(retention, "delete_feedback_event", original)
    assert (
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 2, tzinfo=UTC))[
            "feedback"
        ]
        == 2
    )
    with app.state.session_factory() as db:
        assert check_integrity(db) == []


def test_migration_0012_upgrade_and_downgrade_keep_events(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "0011_semantic_dedup")
    from kebivo.db import make_engine

    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        stamp = "2025-01-01T00:00:00+00:00"
        connection.execute(
            text(
                "INSERT INTO projects(id,name,slug,created_at,updated_at) "
                "VALUES ('p','Existing','existing',:stamp,:stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO feedback_events("
                "id,project_id,submitted_via,event_type,question,"
                "normalized_question_hash,created_at,updated_at) "
                "VALUES ('e','p','rest','knowledge_gap','preserved content',:hash,:stamp,:stamp)"
            ),
            {"hash": "a" * 64, "stamp": stamp},
        )
        before = connection.exec_driver_sql(
            "SELECT question FROM feedback_events WHERE id='e'"
        ).scalar_one()
    engine.dispose()

    command.upgrade(config, "0012_retention")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        indexes = {
            row[1] for row in connection.exec_driver_sql("PRAGMA index_list('delivery_attempts')")
        }
        assert "ix_delivery_attempts_completed" in indexes
        assert (
            connection.exec_driver_sql(
                "SELECT question FROM feedback_events WHERE id='e'"
            ).scalar_one()
            == before
        )
    engine.dispose()
    command.downgrade(config, "0011_semantic_dedup")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT question FROM feedback_events WHERE id='e'"
            ).scalar_one()
            == before
        )
        assert "maintenance_runs" not in {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    engine.dispose()


def test_cli_dry_run_preserves_database_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, app, a, _ = setup(tmp_path)
    seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC))
    path = tmp_path / "retention.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("sys.argv", ["kebivo.cli", "retention", "--dry-run"])
    cli_main()
    assert json.loads(capsys.readouterr().out)["feedback"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_cli_version_reads_project_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["kebivo.cli", "version"])
    cli_main()
    assert capsys.readouterr().out.strip() == "0.2.0"


def test_orphan_event_is_skipped_without_stopping_the_run(tmp_path: Path) -> None:
    _, app, a, _ = setup(tmp_path)
    old = datetime(2025, 1, 1, tzinfo=UTC)
    before = seed(app, a["id"], old - timedelta(seconds=1), "before")
    orphan = seed(app, a["id"], old, "orphan")
    after = seed(app, a["id"], old + timedelta(seconds=1), "after")
    with app.state.session_factory() as db:
        member = db.get(FeedbackGroupMember, orphan)
        assert member is not None
        group = db.get(FeedbackGroup, member.group_id)
        db.delete(member)
        db.flush()
        db.delete(group)
        db.commit()
    counts = run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC))
    assert counts["feedback"] == 2 and counts["skipped_error"] == 1
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, before) is None and db.get(FeedbackEvent, after) is None
        assert db.get(FeedbackEvent, orphan) is not None
        latest = db.scalar(select(MaintenanceRun))
        assert latest is not None and latest.status == "completed"


def test_stop_event_cancels_run_between_batches(tmp_path: Path) -> None:
    import threading

    from kebivo.retention import RetentionCancelled

    _, app, a, _ = setup(tmp_path)
    kept = seed(app, a["id"], datetime(2025, 1, 1, tzinfo=UTC))
    stop = threading.Event()
    stop.set()
    with pytest.raises(RetentionCancelled):
        run(app.state.session_factory, app.state.settings, datetime(2026, 1, 1, tzinfo=UTC), stop)
    with app.state.session_factory() as db:
        assert db.get(FeedbackEvent, kept) is not None
        latest = db.scalar(select(MaintenanceRun))
        assert latest is not None and latest.status == "failed"
        assert latest.error_category == "RetentionCancelled"
        assert due(db, datetime(2026, 1, 1, tzinfo=UTC))
