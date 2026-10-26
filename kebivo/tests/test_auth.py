from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from kebivo.app import create_app
from kebivo.auth import COOKIE_NAME, _digest, _peer_ip, create_admin
from kebivo.models import AdminSession, AdminUser, AuditEvent, Base
from kebivo.settings import Settings

ORIGIN = "https://admin.example.test"


def make_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    settings = Settings(
        "Kebivo",
        "test",
        "test-secret-key-for-auth-tests-000000",
        str(tmp_path / "auth.db"),
        allowed_origins=(ORIGIN,),
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        create_admin(db, "Admin@Example.test", "long-test-password")
    return TestClient(app, base_url=ORIGIN), app


def test_login_session_cookie_csrf_logout_and_session_revocation(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    headers = {"Origin": ORIGIN}
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers=headers,
    )
    assert login.status_code == 200
    cookie = login.cookies[COOKIE_NAME]
    assert cookie
    assert login.headers["cache-control"] == "no-store"
    set_cookie = login.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    assert "path=/" in set_cookie and "domain=" not in set_cookie
    assert "max-age=28800" in set_cookie
    with app.state.session_factory() as db:
        row = db.scalar(select(AdminSession))
        assert row is not None and row.token_hash == _digest(cookie)
        assert row.token_hash != cookie

    current = client.get("/api/v1/auth/session")
    assert current.status_code == 200
    csrf = current.json()["csrf_token"]
    rejected = client.post("/api/v1/auth/logout", headers=headers)
    assert rejected.status_code == 403
    logout = client.post("/api/v1/auth/logout", headers={**headers, "X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert client.get("/api/v1/auth/session").status_code == 401
    with app.state.session_factory() as db:
        row = db.scalar(select(AdminSession))
        assert row is not None and row.revoked_at is not None
        actions = set(db.scalars(select(AuditEvent.action)).all())
        assert {"admin.bootstrap", "admin.login_succeeded", "admin.logout"} <= actions
    app.state.engine.dispose()


def test_origin_is_required_and_no_signup_endpoint_exists(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    missing = client.post(
        "/api/v1/auth/login", json={"email": "x@y.test", "password": "anything"}
    )
    assert missing.status_code == 403
    assert ORIGIN in missing.json()["error"]["message"]
    assert client.post("/api/v1/auth/signup", headers={"Origin": ORIGIN}).status_code >= 400
    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": "x@y.test", "password": "anything"},
        headers={"Origin": "https://evil.example.test"},
    )
    assert wrong.status_code == 403
    assert ORIGIN in wrong.json()["error"]["message"]
    assert "evil.example.test" not in wrong.text
    app.state.engine.dispose()


def test_expired_session_and_wrong_origin_are_rejected(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    with app.state.session_factory.begin() as db:
        row = db.scalar(select(AdminSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/v1/auth/session").status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    wrong = client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "https://evil.example.test",
            "X-CSRF-Token": login.json()["csrf_token"],
        },
    )
    assert wrong.status_code == 403
    assert ORIGIN in wrong.json()["error"]["message"]
    assert "evil.example.test" not in wrong.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    active_token_hash = _digest(login.cookies[COOKIE_NAME])
    with app.state.session_factory.begin() as db:
        row = db.scalar(select(AdminSession).where(AdminSession.token_hash == active_token_hash))
        assert row is not None
        row.last_seen_at = datetime.now(UTC) - timedelta(minutes=31)
    assert client.get("/api/v1/auth/session").status_code == 401
    app.state.engine.dispose()


def test_login_throttle_is_bounded_by_identity_and_ip(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "unknown@example.test", "password": "wrong"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "unknown@example.test", "password": "wrong"},
        headers={"Origin": ORIGIN},
    )
    assert limited.status_code == 429
    app.state.engine.dispose()


def test_password_reset_revokes_existing_sessions(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    with app.state.session_factory() as db:
        admin = db.scalar(select(AdminUser))
        assert admin is not None
        create_admin(db, admin.email, "replacement-password", reset=True)
        session = db.scalar(select(AdminSession))
        assert session is not None and session.revoked_at is not None
    assert client.get("/api/v1/auth/session").status_code == 401
    app.state.engine.dispose()


def test_password_reset_clears_login_throttle(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    for _ in range(5):
        client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.test", "password": "wrong"},
            headers={"Origin": ORIGIN},
        )
    good = {"email": "admin@example.test", "password": "replacement-password"}
    with app.state.session_factory() as db:
        create_admin(db, "admin@example.test", "replacement-password", reset=True)
    response = client.post("/api/v1/auth/login", json=good, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    app.state.engine.dispose()


def test_forwarded_ip_is_used_only_from_configured_proxy(tmp_path: Path) -> None:
    settings = Settings(
        "Kebivo",
        "test",
        "test-secret-key-for-auth-tests-000000",
        str(tmp_path / "proxy.db"),
        trusted_proxy_ips=("10.0.0.5",),
    )
    app = create_app(settings)
    trusted = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"203.0.113.9")],
            "client": ("10.0.0.5", 443),
            "app": app,
        }
    )
    untrusted = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"203.0.113.9")],
            "client": ("10.0.0.6", 443),
            "app": app,
        }
    )
    assert _peer_ip(trusted) == "203.0.113.9"
    assert _peer_ip(untrusted) == "10.0.0.6"
    app.state.engine.dispose()
