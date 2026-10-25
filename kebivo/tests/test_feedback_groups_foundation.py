"""Phase 5 group, FTS and migration invariants."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from kebivo.app import _probe_fts5
from kebivo.cli import main as cli_main
from kebivo.db import make_engine
from kebivo.feedback.groups import check_integrity
from kebivo.feedback.routes import GAP_EXAMPLE
from kebivo.models import FeedbackEvent, FeedbackGroup, FeedbackGroupHistory, FeedbackGroupMember
from kebivo.tests.test_feedback_submission import post, setup


def config(path: Path) -> Config:
    result = Config("alembic.ini")
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return result


def test_upgrade_populated_0007_and_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    command.upgrade(config(path), "0007_routing_destinations")
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO projects(id,name,slug,is_active,created_at,updated_at) "
            "VALUES ('p','Project','p',1,'2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,"
            "metadata_json,normalized_question_hash,question_hash_version,processing_status,"
            "created_at,updated_at) VALUES "
            "('e','p','rest','knowledge_gap','Indexed question','{}','hash',1,'pending',"
            "'2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO deliveries(id,event_id,project_id,destination_id,status,"
            "created_at,updated_at) SELECT 'd','e','p',id,'delivered',"
            "'2026-01-01','2026-01-01' FROM destinations WHERE type='inbox'"
        )
        conn.exec_driver_sql(
            "INSERT INTO delivery_attempts(id,delivery_id,attempt_number,trigger,"
            "started_at,result) VALUES ('a','d',1,'auto','2026-01-01','success')"
        )
    engine.dispose()
    command.upgrade(config(path), "head")
    engine = make_engine(f"sqlite:///{path}")
    with Session(engine) as db:
        assert check_integrity(db) == []
        assert db.scalar(
            select(FeedbackGroupMember.group_id).where(FeedbackGroupMember.event_id == "e")
        )
        assert db.scalar(select(FeedbackGroupHistory.action)) == "created"
        assert (
            db.execute(
                text("SELECT event_id FROM feedback_search WHERE feedback_search MATCH 'Indexed'")
            ).scalar_one()
            == "e"
        )
        assert db.execute(text("SELECT COUNT(*) FROM delivery_attempts")).scalar_one() == 1
    engine.dispose()
    command.downgrade(config(path), "0007_routing_destinations")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM feedback_events").scalar_one() == 1
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM delivery_attempts").scalar_one() == 1
        assert "feedback_groups" not in set(
            __import__("sqlalchemy").inspect(engine).get_table_names()
        )
    engine.dispose()


def test_ingest_groups_fts_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    command.upgrade(config(tmp_path / "feedback.db"), "head")
    client, app, _, token = setup(tmp_path)
    first = post(client, token, GAP_EXAMPLE)
    assert first.status_code == 202
    event_id = first.json()["event_id"]
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
        assert db.scalar(select(FeedbackGroup.occurrence_count)) == 1
        assert (
            db.execute(
                text("SELECT event_id FROM feedback_search WHERE event_id=:id"), {"id": event_id}
            ).scalar_one()
            == event_id
        )
        group_id = db.scalar(select(FeedbackGroupMember.group_id))
    detail = client.get(f"/api/v1/admin/feedback/{event_id}").json()
    assert detail["group"] == {"id": group_id, "status": "open"}
    assert client.get("/api/v1/admin/feedback").json()["items"][0]["group_id"] == group_id

    def fail_after_group(db: Session, event: FeedbackEvent) -> None:
        from kebivo.feedback.groups import create_initial_group

        create_initial_group(db, event)
        raise RuntimeError("forced rollback")

    monkeypatch.setattr("kebivo.feedback.service.create_initial_group", fail_after_group)
    with pytest.raises(RuntimeError, match="forced rollback"):
        post(client, token, {**GAP_EXAMPLE, "question": "Rollback canary"})
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
        assert (
            db.execute(
                text("SELECT COUNT(*) FROM feedback_search WHERE question='Rollback canary'")
            ).scalar_one()
            == 0
        )
        assert db.scalar(select(FeedbackGroup.occurrence_count)) == 1
    with app.state.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM feedback_group_members WHERE event_id=?", (event_id,))
        conn.exec_driver_sql("DELETE FROM feedback_groups WHERE id=?", (group_id,))
        conn.exec_driver_sql("DELETE FROM feedback_events WHERE id=?", (event_id,))
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM feedback_search WHERE event_id=?", (event_id,)
            ).scalar_one()
            == 0
        )


def test_sqlite_secure_search_available(tmp_path: Path) -> None:
    assert sqlite3.sqlite_version_info >= (3, 42, 0)
    assert _probe_fts5()
    engine = make_engine(f"sqlite:///{tmp_path / 'secure.db'}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA secure_delete").scalar_one() == 1
    engine.dispose()


def test_migration_0008_refuses_old_sqlite_without_schema_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old-sqlite.db"
    migration = config(path)
    command.upgrade(migration, "0007_routing_destinations")
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 2))
    with pytest.raises(RuntimeError, match="requires SQLite 3.42 or newer"):
        command.upgrade(migration, "0008_feedback_management")
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert after == before
    assert version == "0007_routing_destinations"


def test_check_groups_cli_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "cli.db"
    command.upgrade(config(path), "head")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setattr("sys.argv", ["kebivo-admin", "check-groups"])
    cli_main()
    assert "consistent" in capsys.readouterr().out
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO projects(id,name,slug,is_active,created_at,updated_at) "
            "VALUES ('p','Project','p',1,'2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,"
            "metadata_json,normalized_question_hash,question_hash_version,"
            "processing_status,created_at,updated_at) VALUES "
            "('e','p','rest','knowledge_gap','Question','{}','hash',1,'pending',"
            "'2026-01-01','2026-01-01')"
        )
    engine.dispose()
    with pytest.raises(SystemExit) as error:
        cli_main()
    assert error.value.code == 1
    assert "event e: no group" in capsys.readouterr().err
