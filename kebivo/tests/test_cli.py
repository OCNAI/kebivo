import json
import re
import tomllib
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from kebivo.app import create_app
from kebivo.auth import create_admin
from kebivo.cli import main
from kebivo.db import make_engine
from kebivo.models import ApiToken, AuditEvent, Base, Project
from kebivo.settings import Settings
from kebivo.tokens import authenticate_bearer


def test_bootstrap_cli_completes_and_created_admin_can_log_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    email = "smoke-admin@example.invalid"
    password = "a-long-bootstrap-password"
    database_path = tmp_path / "bootstrap.db"
    database_url = f"sqlite:///{database_path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setattr(
        "sys.argv", ["kebivo-admin", "bootstrap", "--email", email, "--password-stdin"]
    )
    monkeypatch.setattr("sys.stdin", StringIO(f"{password}\n"))

    main()

    assert capsys.readouterr().out == f"Administrator {email} is ready.\n"

    settings = Settings(
        "Kebivo",
        "test",
        "test-secret-key-for-cli-tests-000000",
        str(database_path),
        database_url=database_url,
        allowed_origins=("https://admin.example.test",),
    )
    with TestClient(create_app(settings), base_url="https://admin.example.test") as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers={"Origin": "https://admin.example.test"},
        )

    assert response.status_code == 200


def test_bootstrap_stdin_keeps_password_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_url = f"sqlite:///{tmp_path / 'short.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(
        "sys.argv",
        ["kebivo-admin", "bootstrap", "--email", "admin@example.org", "--password-stdin"],
    )
    monkeypatch.setattr("sys.stdin", StringIO("short\n"))
    with pytest.raises(SystemExit, match="at least 12 characters"):
        main()
    assert "GetPassWarning" not in capsys.readouterr().err


def test_project_create_cli_issues_token_and_rejects_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_url = f"sqlite:///{tmp_path / 'project.db'}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        create_admin(db, "admin@example.org", "long-test-password")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-cli-tests-000000")
    monkeypatch.setattr(
        "sys.argv", ["kebivo-admin", "project", "create", "--name", "Helpdesk bot", "--json"]
    )
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["slug"] == "helpdesk-bot"
    assert result["token"].startswith("kebivo_pt_")
    with Session(engine) as db:
        project = db.scalar(select(Project).where(Project.slug == "helpdesk-bot"))
        assert project is not None and project.id == result["id"]
        token = db.scalar(select(ApiToken).where(ApiToken.project_id == project.id))
        assert token is not None and json.loads(token.scopes_json) == ["feedback:gap"]
        assert result["token"] not in token.token_hash
        assert (
            authenticate_bearer(db, Settings.from_env(), result["token"])[0].project_id
            == project.id
        )
        actions = [
            row.action for row in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
        ]
        assert actions.index("project.created") < actions.index("project_token.created")
    with pytest.raises(SystemExit, match="slug already exists"):
        main()
    assert capsys.readouterr().out == ""
    monkeypatch.setattr("sys.argv", ["kebivo-admin", "project", "create", "--name", " "])
    with pytest.raises(SystemExit, match="Invalid project"):
        main()
    for name in ("!!!", "東京"):
        monkeypatch.setattr("sys.argv", ["kebivo-admin", "project", "create", "--name", name])
        with pytest.raises(SystemExit, match="Could not derive a slug from the name; pass --slug"):
            main()
    monkeypatch.setattr(
        "sys.argv", ["kebivo-admin", "project", "create", "--name", "Other", "--slug", "custom"]
    )
    main()
    output = capsys.readouterr().out
    assert "Project Other (custom) created." in output
    assert output.count("kebivo_pt_") == 1
    monkeypatch.setattr(
        "sys.argv", ["kebivo-admin", "project", "create", "--name", "Other", "--slug", "bad!"]
    )
    with pytest.raises(SystemExit, match="Invalid project"):
        main()
    monkeypatch.setattr(
        "sys.argv", ["kebivo-admin", "project", "create", "--name", "Other", "--slug", ""]
    )
    with pytest.raises(SystemExit, match="Invalid project"):
        main()
    engine.dispose()


def test_quickstart_image_tags_match_project_version() -> None:
    root = Path(__file__).resolve().parents[2]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    for path in (root / "README.md", root / "docs/quickstart.md"):
        tags = re.findall(r"ghcr\.io/ocnai/kebivo:([\w.\-]+)", path.read_text())
        assert tags and set(tags) == {version}, path
