from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from kebivo.db import make_engine, transaction
from kebivo.models import User


def _alembic_config(db_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_sqlite_configuration_and_schema_are_isolated(tmp_path: Path) -> None:
    db_path = tmp_path / "isolated.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000

        names = set(inspect(engine).get_table_names())
        assert {
            "users",
            "sources",
            "records",
            "processing_jobs",
            "deliveries",
            "idempotency_records",
            "feedback_events",
            "feedback_event_tags",
            "audit_events",
        } <= names
        indexes = {idx["name"] for idx in inspect(engine).get_indexes("records")}
        assert "ix_records_owner_created" in indexes

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with transaction(factory) as session:
            session.add(User(email="owner@example.invalid", password_hash="test-hash"))
        with factory() as session:
            assert session.execute(text("select count(*) from users")).scalar_one() == 1
    finally:
        engine.dispose()
    assert db_path.exists()


def test_empty_database_upgrades_to_auth_project_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "0001_initial")
    phase_zero_engine = make_engine(f"sqlite:///{db_path}")
    try:
        phase_zero_tables = set(inspect(phase_zero_engine).get_table_names())
    finally:
        phase_zero_engine.dispose()
    assert "admin_users" not in phase_zero_tables

    command.upgrade(config, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        assert {
            "users",
            "sources",
            "records",
            "processing_jobs",
            "deliveries",
            "idempotency_records",
            "feedback_events",
            "feedback_event_tags",
            "audit_events",
            "admin_users",
            "admin_sessions",
            "login_attempts",
            "projects",
            "tags",
            "api_tokens",
        } <= names
        assert {col["name"] for col in inspector.get_columns("audit_events")} >= {
            "actor_id",
            "actor_type",
            "actor_ref",
        }
        assert {col["name"] for col in inspector.get_columns("projects")} >= {
            "settings_json",
            "archived_at",
        }
        assert inspector.get_columns("admin_users")[3]["default"] == "1"
        assert "last_login_at" in {
            column["name"] for column in inspector.get_columns("admin_users")
        }
        assert inspector.get_columns("login_attempts")[3]["default"] == "0"
        assert inspector.get_columns("tags")[4]["default"] == "0"
        assert "ix_admin_sessions_admin_expires" in {
            idx["name"] for idx in inspector.get_indexes("admin_sessions")
        }
        assert "ix_login_attempts_email_time" in {
            idx["name"] for idx in inspector.get_indexes("login_attempts")
        }
        assert "ix_login_attempts_attempted_at" in {
            idx["name"] for idx in inspector.get_indexes("login_attempts")
        }
        assert "ix_api_tokens_project_revoked" in {
            idx["name"] for idx in inspector.get_indexes("api_tokens")
        }
        foreign_keys = {fk["referred_table"] for fk in inspector.get_foreign_keys("api_tokens")}
        assert {"projects", "admin_users"} <= foreign_keys
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == ScriptDirectory.from_config(config).get_current_head()
            )
    finally:
        engine.dispose()


def test_populated_phase_zero_database_upgrades_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "populated.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "0001_initial")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            user = User(
                id="legacy-user", email="owner@example.invalid", password_hash="legacy-hash"
            )
            session.add(user)
            session.flush()
            session.execute(
                text("""INSERT INTO audit_events
                    (id, actor_id, action, subject_type, subject_id, details_json, created_at)
                    VALUES ('legacy-audit', 'legacy-user', 'created', 'source',
                    'legacy-source', '{}',
                    '2026-01-01 00:00:00')""")
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT email FROM users WHERE id='legacy-user'")
                ).scalar_one()
                == "owner@example.invalid"
            )
            audit = connection.execute(
                text(
                    "SELECT actor_id, actor_type, actor_ref FROM audit_events "
                    "WHERE id='legacy-audit'"
                )
            ).one()
            assert tuple(audit) == ("legacy-user", "user", None)
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            with pytest.raises(IntegrityError):
                with engine.begin() as transaction:
                    transaction.execute(
                        text(
                            "INSERT INTO admin_sessions "
                            "(id, admin_user_id, token_hash, expires_at, created_at) "
                            "VALUES ('bad', 'missing', 'hash', '2026-01-01', '2026-01-01')"
                        )
                    )
    finally:
        engine.dispose()


def test_revocation_migration_adds_column_to_already_migrated_auth_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "auth-schema.sqlite"
    config = _alembic_config(db_path)
    command.upgrade(config, "0002_auth_projects")
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE admin_sessions DROP COLUMN revoked_at")
            connection.exec_driver_sql("DROP INDEX ix_login_attempts_attempted_at")
            connection.exec_driver_sql("ALTER TABLE admin_users DROP COLUMN last_login_at")
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        assert "revoked_at" in {
            column["name"] for column in inspect(engine).get_columns("admin_sessions")
        }
        assert "last_login_at" in {
            column["name"] for column in inspect(engine).get_columns("admin_users")
        }
        assert "ix_login_attempts_attempted_at" in {
            index["name"] for index in inspect(engine).get_indexes("login_attempts")
        }
    finally:
        engine.dispose()


def _schema(db_path: Path) -> list[tuple[str, str, str]]:
    engine = make_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            return [
                (row.type, row.name, row.sql)
                for row in connection.exec_driver_sql(
                    "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','index') "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ]
    finally:
        engine.dispose()


def test_fresh_and_populated_0005_upgrades_match(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    populated = tmp_path / "populated-0005.db"
    command.upgrade(_alembic_config(fresh), "head")
    command.upgrade(_alembic_config(populated), "0005_project_token_scopes")
    engine = make_engine(f"sqlite:///{populated}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO projects (id,name,slug,is_active,created_at,updated_at) "
                "VALUES ('p','Project','project',1,'2026-01-01','2026-01-01')"
            )
            connection.exec_driver_sql(
                "INSERT INTO tags (id,project_id,name,slug,is_default,created_at) "
                "VALUES ('t','p','Bug','bug',1,'2026-01-01')"
            )
    finally:
        engine.dispose()
    command.upgrade(_alembic_config(populated), "head")
    assert _schema(fresh) == _schema(populated)
    engine = make_engine(f"sqlite:///{populated}")
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT is_default FROM tags WHERE id='t'").scalar() == 0
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert "record_id" not in {
                c["name"] for c in inspect(engine).get_columns("processing_jobs")
            }
            assert inspect(engine).get_columns("tags")[1]["nullable"] is False
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["idempotency_keys", "project_tags", "processing_jobs"])
def test_0006_rejects_legacy_data(tmp_path: Path, table: str) -> None:
    path = tmp_path / f"{table}.db"
    config = _alembic_config(path)
    command.upgrade(config, "0005_project_token_scopes")
    engine = make_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            if table == "idempotency_keys":
                connection.exec_driver_sql(
                    "INSERT INTO idempotency_keys (key,scope,created_at) "
                    "VALUES ('k','s','2026-01-01')"
                )
            elif table == "project_tags":
                connection.exec_driver_sql(
                    "INSERT INTO projects (id,name,slug,is_active,created_at,updated_at) "
                    "VALUES ('p','P','p',1,'2026-01-01','2026-01-01')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO tags (id,project_id,name,slug,is_default,created_at) "
                    "VALUES ('t','p','Tag','tag',0,'2026-01-01')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO project_tags (project_id,tag_id,created_at) "
                    "VALUES ('p','t','2026-01-01')"
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO users (id,email,password_hash,created_at) "
                    "VALUES ('u','u@example.test','h','2026-01-01')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO sources (id,owner_id,kind,name,config_json,created_at) "
                    "VALUES ('s','u','test','S','{}','2026-01-01')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO records (id,owner_id,content,metadata_json,created_at) "
                    "VALUES ('r','u','R','{}','2026-01-01')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO processing_jobs "
                    "(id,record_id,status,attempts,created_at,updated_at) "
                    "VALUES ('j','r','queued',0,'2026-01-01','2026-01-01')"
                )
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match=table):
        command.upgrade(config, "head")


def test_0006_rejects_unscoped_tags(tmp_path: Path) -> None:
    path = tmp_path / "unscoped.db"
    config = _alembic_config(path)
    command.upgrade(config, "0005_project_token_scopes")
    engine = make_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO tags (id,name,slug,is_default,created_at) "
                "VALUES ('t','Unscoped','unscoped',0,'2026-01-01')"
            )
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="project_id contains NULL"):
        command.upgrade(config, "head")
