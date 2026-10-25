from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kebivo.app import create_app
from kebivo.cli import main
from kebivo.db import make_engine
from kebivo.models import Base
from kebivo.settings import Settings


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
    monkeypatch.setattr("sys.stdin", StringIO(f"{password}\n{password}\n"))

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
