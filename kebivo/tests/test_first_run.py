import errno
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

from kebivo.app import create_app
from kebivo.auth import create_admin, origin_allowed
from kebivo.first_run import generate
from kebivo.models import AdminUser, Base
from kebivo.settings import Settings, SettingsError


@pytest.mark.parametrize(
    "case", ["key_mismatch", "existing_database", "unwritable_data", "not_a_url"]
)
def test_entrypoint_settings_errors_are_one_line(tmp_path: Path, case: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    secrets_file = data / "kebivo-secrets.env"
    database = data / "app.sqlite3"
    env = {
        **os.environ,
        "APP_ENV": "production",
        "KEBIVO_SECRETS_FILE": str(secrets_file),
        "DATABASE_URL": f"sqlite:///{database}",
        "SECRET_KEY": "",
        "ENCRYPTION_KEY": "",
    }
    if case == "key_mismatch":
        secrets_file.write_text("SECRET_KEY=" + "s" * 40 + "\nENCRYPTION_KEY=" + "ab" * 32 + "\n")
        secrets_file.chmod(0o600)
        env["SECRET_KEY"] = "t" * 40
        expected = (
            "SECRET_KEY differs from the key in KEBIVO_SECRETS_FILE; to change it on purpose, "
            f"delete the SECRET_KEY line from {secrets_file}"
        )
    elif case == "existing_database":
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE alembic_version (version_num TEXT)")
            db.execute("INSERT INTO alembic_version VALUES ('0016')")
        expected = "SECRET_KEY is required for an existing database or secrets file"
    elif case == "not_a_url":
        env["DATABASE_URL"] = "not-a-url"
        expected = "DATABASE_URL must be a SQLite URL, such as sqlite:////data/app.sqlite3"
    else:
        data.rmdir()
        data.write_text("not a directory")
        expected = "Cannot write secrets file (Not a directory); set SECRET_KEY and ENCRYPTION_KEY"

    result = subprocess.run(
        ["sh", "docker/entrypoint.sh"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"SettingsError: {expected}\n"
    assert "Traceback" not in result.stderr
    assert "s" * 40 not in result.stderr
    assert "t" * 40 not in result.stderr
    assert "ab" * 32 not in result.stderr


def test_app_import_settings_error_is_one_line(tmp_path: Path) -> None:
    secrets_file = tmp_path / "kebivo-secrets.env"
    secrets_file.write_text("SECRET_KEY=" + "s" * 40 + "\nENCRYPTION_KEY=" + "ab" * 32 + "\n")
    secrets_file.chmod(0o600)
    env = {
        **os.environ,
        "APP_ENV": "production",
        "KEBIVO_SECRETS_FILE": str(secrets_file),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.sqlite3'}",
        "SECRET_KEY": "t" * 40,
        "ENCRYPTION_KEY": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import kebivo.app"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "SettingsError: SECRET_KEY differs from the key in KEBIVO_SECRETS_FILE; "
        f"to change it on purpose, delete the SECRET_KEY line from {secrets_file}\n"
    )
    assert "s" * 40 not in result.stderr
    assert "t" * 40 not in result.stderr
    assert "ab" * 32 not in result.stderr


def test_generated_keys_are_atomic_and_visible_to_new_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "secrets.env"
    env = {
        "KEBIVO_SECRETS_FILE": str(path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite3'}",
        "APP_ENV": "production",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(generate, [env, env]))
    assert not caplog.text
    original = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(original.splitlines()) == 2
    assert all(len(line.split("=", 1)[1]) == 64 for line in original.splitlines())
    generate(env)
    assert path.read_text() == original
    configured = Settings.from_env(env)
    assert configured.secret_key == original.splitlines()[0].split("=", 1)[1]
    for name, other in (("SECRET_KEY", "z" * 40), ("ENCRYPTION_KEY", "ab" * 32)):
        with pytest.raises(SettingsError, match=f"delete the {name} line") as mismatch:
            Settings.from_env({**env, name: other})
        assert other not in str(mismatch.value)
        assert configured.secret_key not in str(mismatch.value)
        assert configured.encryption_key not in str(mismatch.value)
    same = {**env, "SECRET_KEY": configured.secret_key, "ENCRYPTION_KEY": configured.encryption_key}
    assert Settings.from_env(same).encryption_key == configured.encryption_key
    blank_override = {**env, "SECRET_KEY": "", "ENCRYPTION_KEY": ""}
    assert Settings.from_env(blank_override).secret_key == configured.secret_key
    assert Settings.from_env(blank_override).encryption_key == configured.encryption_key
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from kebivo.settings import Settings; "
            "s=Settings.from_env(); print(s.secret_key, s.encryption_key)",
        ],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"{configured.secret_key} {configured.encryption_key}"
    assert configured.secret_key not in result.stderr
    assert configured.encryption_key not in result.stderr
    with pytest.raises(SettingsError) as error:
        Settings.from_env({**env, "ENCRYPTION_KEY": "private-invalid-value"})
    assert "private-invalid-value" not in str(error.value)
    assert configured.secret_key not in caplog.text
    assert configured.encryption_key not in caplog.text


def test_existing_database_never_gets_new_keys(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    database = tmp_path / "db.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE alembic_version (version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('0016')")
    env = {"KEBIVO_SECRETS_FILE": str(path), "DATABASE_URL": f"sqlite:///{database}"}
    with pytest.raises(SettingsError, match="SECRET_KEY") as error:
        generate(env)
    assert not path.exists()
    assert "=" not in str(error.value)
    for override in (
        {"DATABASE_PATH": str(tmp_path / "elsewhere")},
        {"DATABASE_URL": f"sqlite:///{database}?timeout=5"},
    ):
        with pytest.raises(SettingsError, match="SECRET_KEY"):
            generate({**env, **override})
        assert not path.exists()


def test_blank_environment_keys_use_generated_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    env = {
        "KEBIVO_SECRETS_FILE": str(path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite3'}",
        "APP_ENV": "production",
        "SECRET_KEY": "",
        "ENCRYPTION_KEY": "",
    }
    generate(env)
    configured = Settings.from_env(env)
    assert configured.secret_key and configured.encryption_key
    assert path.exists()


def test_one_generated_key_is_logged_by_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "secrets.env"
    env = {
        "KEBIVO_SECRETS_FILE": str(path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite3'}",
        "APP_ENV": "production",
        "SECRET_KEY": "z" * 40,
    }
    generate(env)
    (line,) = path.read_text().splitlines()
    assert line.startswith("ENCRYPTION_KEY=")
    assert "Generated ENCRYPTION_KEY" in caplog.text
    assert line.split("=", 1)[1] not in caplog.text
    configured = Settings.from_env(env)
    assert configured.secret_key == "z" * 40


def test_secrets_file_without_hard_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "secrets.env"
    env = {
        "KEBIVO_SECRETS_FILE": str(path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite3'}",
        "APP_ENV": "production",
    }

    def no_link(source: str, target: Path) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", no_link)
    generate(env)
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(path.read_text().splitlines()) == 2
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".kebivo-")] == []
    assert Settings.from_env(env).secret_key

    def denied(source: str, target: Path) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    path.unlink()
    monkeypatch.setattr(os, "link", denied)
    with pytest.raises(SettingsError, match="uid 10001"):
        generate(env)
    assert not path.exists()


def test_public_file_key_warning_only_on_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "secrets.env"
    env = {
        "KEBIVO_SECRETS_FILE": str(path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite3'}",
        "APP_ENV": "production",
        "ALLOWED_ORIGINS": "https://console.example",
    }
    generate(env)
    settings = Settings.from_env(env)
    Settings.from_env(env)
    assert "File-backed keys are in use" not in caplog.text
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app):
        pass
    assert caplog.text.count("File-backed keys are in use") == 1


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://localhost:8000", "localhost:8000", True),
        ("http://localhost:9000", "localhost:9000", True),
        ("http://[::1]:9000", "[::1]:9000", True),
        ("http://evil.example", "evil.example", False),
        ("http://localhost:3000", "localhost:8000", False),
        ("http://localhost:8000", "evil.example", False),
        ("https://localhost:8000", "localhost:8000", False),
    ],
)
def test_loopback_origin(origin: str, host: str, allowed: bool) -> None:
    class App:
        class State:
            settings = Settings("Kebivo", "test", None, ":memory:")

        state = State()

    request = Request(
        {
            "type": "http",
            "app": App(),
            "headers": [(b"origin", origin.encode()), (b"host", host.encode())],
        }
    )
    assert origin_allowed(request) is allowed
    App.state.settings = Settings(
        "Kebivo", "test", None, ":memory:", allowed_origins=("https://console.example",)
    )
    assert origin_allowed(request) is False
    configured_request = Request(
        {"type": "http", "app": App(), "headers": [(b"origin", b"https://console.example")]}
    )
    assert origin_allowed(configured_request) is True


def test_setup_token_refusals_and_single_claim(tmp_path: Path) -> None:
    settings = Settings("Kebivo", "test", "s" * 40, str(tmp_path / "app.db"), worker_enabled=False)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url="http://localhost:8000") as client:
        token = app.state.setup_token
        assert token and client.get("/api/v1/auth/setup").json() == {"required": True}
        body = {"token": token, "email": "a@example.test", "password": "long-test-password"}
        headers = {"Origin": "http://localhost:8000"}
        assert (
            client.post(
                "/api/v1/auth/setup", json={**body, "token": ""}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/auth/setup", json={**body, "password": "short"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/auth/setup", json={**body, "token": "wrong"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/auth/setup", json={**body, "token": "é"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/auth/setup", json={**body, "email": "     "}, headers=headers
            ).status_code
            == 422
        )
        wrong_origin = client.post(
            "/api/v1/auth/setup", json=body, headers={"Origin": "http://evil.example"}
        )
        assert wrong_origin.status_code == 403
        assert "set ALLOWED_ORIGINS" in wrong_origin.text
        assert "Configured origins: none" not in wrong_origin.text
        app.state.setup_deadline = time.monotonic() - 1
        assert client.post("/api/v1/auth/setup", json=body, headers=headers).status_code == 403
        app.state.setup_deadline = time.monotonic() + 3600

        def claim(email: str) -> int:
            return client.post(
                "/api/v1/auth/setup", json={**body, "email": email}, headers=headers
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(claim, ["a@example.test", "b@example.test"]))
        assert sorted(statuses) == [200, 403] or sorted(statuses) == [200, 409]
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(AdminUser)) == 1
        assert client.get("/api/v1/auth/setup").json() == {"required": False}
        assert client.post("/api/v1/auth/setup", json=body, headers=headers).status_code in {
            403,
            409,
        }
    app.state.engine.dispose()


def test_setup_refused_after_cli_bootstrap(tmp_path: Path) -> None:
    settings = Settings("Kebivo", "test", "s" * 40, str(tmp_path / "app.db"), worker_enabled=False)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app, base_url="http://localhost:8000") as client:
        token = app.state.setup_token
        assert token
        with app.state.session_factory() as db:
            create_admin(db, "cli@example.test", "long-test-password")
        assert client.get("/api/v1/auth/setup").json() == {"required": False}
        response = client.post(
            "/api/v1/auth/setup",
            json={"token": token, "email": "web@example.test", "password": "long-test-password"},
            headers={"Origin": "http://localhost:8000"},
        )
        assert response.status_code == 409
    app.state.engine.dispose()
