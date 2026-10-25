"""Phase 4 schema and secret-storage invariants."""

import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from kebivo.db import make_engine
from kebivo.models import Base
from kebivo.security.secretbox import SecretBox, SecretError, mask
from kebivo.settings import Settings, SettingsError


def config(path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return cfg


@pytest.mark.parametrize("table", ["records", "deliveries"])
def test_migration_refuses_legacy_rows(tmp_path: Path, table: str) -> None:
    path = tmp_path / "legacy.db"
    command.upgrade(config(path), "0006_feedback_ingestion")
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO users (id,email,password_hash,created_at) "
            "VALUES ('u','u@example.org','h','2026-01-01')"
        )
        if table == "records":
            conn.exec_driver_sql(
                "INSERT INTO records (id,owner_id,content,metadata_json,created_at) "
                "VALUES ('r','u','text','{}','2026-01-01')"
            )
        else:
            conn.exec_driver_sql(
                "INSERT INTO records (id,owner_id,content,metadata_json,created_at) "
                "VALUES ('r','u','text','{}','2026-01-01')"
            )
            conn.exec_driver_sql(
                "INSERT INTO deliveries (id,record_id,destination,status,attempts,created_at) "
                "VALUES ('d','r','x','queued',0,'2026-01-01')"
            )
    engine.dispose()
    with pytest.raises(RuntimeError, match="contains rows"):
        command.upgrade(config(path), "head")


def test_upgrade_preserves_phase_two_and_models_match(tmp_path: Path) -> None:
    path = tmp_path / "phase-two.db"
    command.upgrade(config(path), "0006_feedback_ingestion")
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO projects (id,name,slug,is_active,created_at,updated_at) "
            "VALUES ('p','Project','p',1,'2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_events (id,project_id,submitted_via,event_type,question,"
            "metadata_json,normalized_question_hash,question_hash_version,processing_status,"
            "created_at,updated_at) VALUES ('e','p','rest','knowledge_gap','Question',"
            "'{}','hash',1,'pending','2026-01-01','2026-01-01')"
        )
    engine.dispose()
    command.upgrade(config(path), "head")
    engine = make_engine(f"sqlite:///{path}")
    inspector = inspect(engine)
    for name in (
        "destinations",
        "routing_rules",
        "routing_rule_tags",
        "routing_rule_destinations",
        "routing_matches",
        "deliveries",
        "delivery_attempts",
    ):
        actual = {col["name"] for col in inspector.get_columns(name)}
        expected = set(Base.metadata.tables[name].columns.keys())
        assert actual == expected
    with engine.connect() as conn:
        assert (
            conn.exec_driver_sql("SELECT question FROM feedback_events WHERE id='e'").scalar_one()
            == "Question"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM destinations WHERE type='inbox' "
                "AND is_system=1 AND enabled=1 AND name='Built-in inbox'"
            ).scalar_one()
            == 1
        )
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_secretbox_roundtrip_tamper_row_binding_wrong_key_rotation() -> None:
    first = SecretBox("12" * 32)
    second = SecretBox("34" * 32, "12" * 32)
    blob = first.encrypt("destination-1", {"url": "https://example.invalid/token"})
    assert first.decrypt("destination-1", blob)["url"].endswith("/token")
    assert mask({"url": "secret"}) == {"url": {"set": True}}
    assert second.decrypt("destination-1", blob) == first.decrypt("destination-1", blob)
    assert second.encrypt("destination-1", {"token": "new"}).split(":")[1] == second.current_key_id
    for bad_id, bad_blob, box in [
        ("destination-2", blob, first),
        ("destination-1", blob[:-2] + "AA", first),
        ("destination-1", blob, SecretBox("56" * 32)),
    ]:
        with pytest.raises(SecretError, match="cannot be decrypted"):
            box.decrypt(bad_id, bad_blob)


def test_settings_validate_keys_and_worker_limits_without_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(SettingsError, match="ENCRYPTION_KEY is required"):
        Settings.from_env({"APP_ENV": "production", "SECRET_KEY": "s" * 40})
    canary = "bad-secret-canary"
    with pytest.raises(SettingsError) as invalid:
        Settings.from_env({"ENCRYPTION_KEY": canary})
    assert canary not in str(invalid.value)
    with pytest.raises(SettingsError):
        Settings.from_env({"ENCRYPTION_KEY": "12" * 32, "SECRET_KEY": "12" * 32})
    for name, value in [
        ("WORKER_CONCURRENCY", "33"),
        ("WORKER_POLL_INTERVAL_SECONDS", "0"),
        ("WORKER_SHUTDOWN_GRACE_SECONDS", "121"),
        ("DELIVERY_RETRY_SCHEDULE_SECONDS", "1,2,3,4,5,6"),
    ]:
        with pytest.raises(SettingsError):
            Settings.from_env({name: value})
    with caplog.at_level(logging.WARNING):
        dev = Settings.from_env({"APP_ENV": "development"})
    assert len(dev.encryption_key) == 64
    assert dev.encryption_key not in caplog.text


def test_startup_rejects_unavailable_key_with_ids_only(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from kebivo.app import create_app
    from kebivo.models import Destination

    path = tmp_path / "startup.db"
    command.upgrade(config(path), "head")
    engine = make_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        session.add(
            Destination(
                id="destination-canary",
                name="Secret name must not appear",
                type="webhook",
                config_json="{}",
                secrets_ciphertext="secret-ciphertext-canary",
                secrets_key_id="deadbeef",
            )
        )
        session.commit()
    engine.dispose()
    settings = Settings(
        "test", "test", None, str(path), database_url=f"sqlite:///{path}", encryption_key="12" * 32
    )
    with pytest.raises(RuntimeError) as failure:
        with TestClient(create_app(settings)):
            pass
    assert "destination-canary" in str(failure.value)
    assert "Secret name" not in str(failure.value)
    assert "secret-ciphertext-canary" not in str(failure.value)


def test_reencrypt_cli_rotates_all_rows_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.orm import Session

    from kebivo.cli import main
    from kebivo.models import Destination, LlmProviderConfig

    path = tmp_path / "rotate.db"
    command.upgrade(config(path), "head")
    engine = make_engine(f"sqlite:///{path}")
    old = SecretBox("12" * 32)
    new = SecretBox("34" * 32, "12" * 32)
    with Session(engine) as db:
        db.add(
            Destination(
                id="rotate-one",
                name="Rotating",
                type="webhook",
                config_json="{}",
                secrets_ciphertext=old.encrypt("rotate-one", {"token": "canary-value"}),
                secrets_key_id=old.current_key_id,
            )
        )
        db.add(
            LlmProviderConfig(
                id="rotate-provider",
                name="Rotating provider",
                provider_type="openai_compatible",
                base_url="https://example.com/v1",
                model="example-model",
                structured_output="json_schema",
                secrets_ciphertext=old.encrypt(
                    "rotate-provider",
                    {"api_key": "provider-canary"},
                    purpose="llm-provider-secrets",
                ),
                secrets_key_id=old.current_key_id,
            )
        )
        db.commit()
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("ENCRYPTION_KEY", "34" * 32)
    monkeypatch.setenv("ENCRYPTION_KEY_PREVIOUS", "12" * 32)
    monkeypatch.setattr("sys.argv", ["kebivo.cli", "reencrypt-secrets"])
    main()
    assert capsys.readouterr().out == (
        "Re-encrypted 1 destination secrets and 1 LLM provider secrets.\n"
    )
    engine = make_engine(f"sqlite:///{path}")
    with Session(engine) as db:
        row = db.get(Destination, "rotate-one")
        assert row is not None
        assert row.secrets_key_id == new.current_key_id
        assert row.secrets_ciphertext is not None
        assert SecretBox("34" * 32).decrypt("rotate-one", row.secrets_ciphertext) == {
            "token": "canary-value"
        }
        provider = db.get(LlmProviderConfig, "rotate-provider")
        assert provider is not None and provider.secrets_ciphertext is not None
        assert provider.secrets_key_id == new.current_key_id
        assert SecretBox("34" * 32).decrypt(
            provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
        ) == {"api_key": "provider-canary"}
    engine.dispose()
