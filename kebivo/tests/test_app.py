import logging
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kebivo.app import create_app
from kebivo.settings import Settings, SettingsError


def test_liveness_and_readiness(tmp_path: Path) -> None:
    app = create_app(Settings("Kebivo", "test", None, str(tmp_path / "app.db")))
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok", "database": "ok", "search": "ok", "worker": "disabled",
        "routing_backlog": {"pending_count": 0, "oldest_pending_age_seconds": None},
    }
    assert live.headers["x-request-id"]


def test_readiness_reports_database_failure_but_liveness_stays_up(tmp_path: Path) -> None:
    app = create_app(Settings("Kebivo", "test", None, str(tmp_path / "db" / "file")))
    app.state.database_path = str(tmp_path / "unwritable-parent" / "db.sqlite")
    # A regular file cannot be used as a directory for SQLite's parent path.
    (tmp_path / "unwritable-parent").write_text("not a directory", encoding="utf-8")

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "worker": "disabled",
    }


def test_readiness_reports_missing_secure_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kebivo.app._probe_fts5", lambda: False)
    with TestClient(create_app(Settings("test", "test", None, ":memory:"))) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["search"] == "unavailable"


def test_request_id_propagates_and_authorization_is_not_logged() -> None:
    app = create_app(Settings("test", "test", None, ":memory:"))
    from kebivo.app import JsonFormatter, _configure_logging

    _configure_logging()
    logger = logging.getLogger("kebivo.request")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    with TestClient(app) as client:
        response = client.get(
            "/health/live?canary=private-query-string-123",
            headers={"X-Request-ID": "trace-123", "Authorization": "Bearer private-token"},
        )
    logger.removeHandler(handler)

    assert response.headers["x-request-id"] == "trace-123"
    rendered_logs = stream.getvalue()
    assert "trace-123" in rendered_logs
    assert "private-token" not in rendered_logs
    assert "authorization" not in rendered_logs.lower()
    assert "private-query-string-123" not in rendered_logs


@pytest.mark.parametrize("secret", [None, "tiny", "changeme"])
def test_production_requires_strong_non_placeholder_secret(secret: str | None) -> None:
    environ = {"APP_ENV": "production"}
    if secret is not None:
        environ["SECRET_KEY"] = secret

    with pytest.raises(SettingsError, match="SECRET_KEY"):
        Settings.from_env(environ)


def test_production_accepts_valid_secret_and_persistent_database() -> None:
    configured = Settings.from_env(
        {"APP_ENV": "production", "SECRET_KEY": "s" * 40, "ENCRYPTION_KEY": "12" * 32}
    )

    assert configured.environment == "production"
    assert configured.secret_key == "s" * 40


def test_production_rejects_non_loopback_http_origin() -> None:
    with pytest.raises(SettingsError, match="HTTPS"):
        Settings.from_env(
            {
                "APP_ENV": "production",
                "SECRET_KEY": "s" * 40,
                "ENCRYPTION_KEY": "12" * 32,
                "ALLOWED_ORIGINS": "http://remote.example.invalid",
            }
        )


def test_allowed_origins_are_parsed_and_validated() -> None:
    configured = Settings.from_env(
        {
            "ALLOWED_ORIGINS": "https://console.example.invalid, http://localhost:5173,https://console.example.invalid/"
        }
    )
    assert configured.allowed_origins == (
        "https://console.example.invalid",
        "http://localhost:5173",
    )
    with pytest.raises(SettingsError, match="ALLOWED_ORIGINS"):
        Settings.from_env({"ALLOWED_ORIGINS": "https://example.invalid/path"})


def test_reserved_namespaces_have_no_business_routes() -> None:
    paths = {
        path
        for route in create_app(Settings("test", "test", None, ":memory:")).routes
        if (path := getattr(route, "path", None)) is not None
    }

    assert "/api/v1" not in paths
    assert "/mcp" in paths
    assert "/mcp" not in create_app(Settings("test", "test", None, ":memory:")).openapi()["paths"]
    assert "/api/v1/anything" not in paths


def test_static_ui_and_dashboard_fallback_keep_api_404_json(tmp_path: Path) -> None:
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        pytest.skip("build the frontend before testing static serving")

    with TestClient(create_app(Settings("test", "test", None, ":memory:"))) as client:
        root = client.get("/")
        dashboard = client.get("/dashboard/settings")
        missing_api = client.get("/api/v1/not-implemented")

    assert root.status_code == dashboard.status_code == 200
    assert "text/html" in dashboard.headers["content-type"]
    assert dashboard.text == root.text
    assert missing_api.status_code == 404
    assert missing_api.json()["error"]["code"] == "not_found"
    assert missing_api.json()["error"]["message"] == "Not Found"


def test_overlong_path_serves_app_shell_without_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("build the frontend before testing static serving")

    with TestClient(create_app(Settings("test", "test", None, ":memory:"))) as client:
        root = client.get("/")
        long = client.get("/" + "a" * 5_000)
    assert long.status_code == 200 and long.text == root.text
    assert "Traceback" not in caplog.text and "File name too long" not in caplog.text
