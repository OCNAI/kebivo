from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.requests import Request

from kebivo.app import create_app
from kebivo.auth import COOKIE_NAME, _digest, _peer_ip, _private_digest, create_admin
from kebivo.models import AdminSession, AdminUser, AuditEvent, Base, LoginAttempt
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
    missing = client.post("/api/v1/auth/login", json={"email": "x@y.test", "password": "anything"})
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


def login_from(app: FastAPI, address: str, email: str, password: str) -> Any:
    client = TestClient(app, base_url=ORIGIN, client=(address, 50000))
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Origin": ORIGIN},
    )


def test_login_lock_holds_one_address_without_locking_the_account(tmp_path: Path) -> None:
    _, app = make_client(tmp_path)
    for _ in range(5):
        assert login_from(app, "192.0.2.10", "admin@example.test", "wrong").status_code == 401
    limited = login_from(app, "192.0.2.10", "admin@example.test", "wrong")
    assert limited.status_code == 429
    assert 890 <= int(limited.headers["retry-after"]) <= 901
    assert limited.headers["retry-after"] in limited.json()["error"]["message"]
    right = login_from(app, "192.0.2.10", "admin@example.test", "long-test-password")
    assert right.status_code == 429
    elsewhere = login_from(app, "192.0.2.20", "admin@example.test", "long-test-password")
    assert elsewhere.status_code == 200
    app.state.engine.dispose()


def test_one_address_trying_many_emails_is_capped(tmp_path: Path) -> None:
    _, app = make_client(tmp_path)
    for index in range(20):
        response = login_from(app, "192.0.2.10", f"user{index}@example.test", "wrong")
        assert response.status_code == 401
    limited = login_from(app, "192.0.2.10", "admin@example.test", "long-test-password")
    assert limited.status_code == 429 and "retry-after" in limited.headers
    elsewhere = login_from(app, "192.0.2.20", "admin@example.test", "long-test-password")
    assert elsewhere.status_code == 200
    app.state.engine.dispose()


def test_guessing_one_email_from_many_addresses_is_slowed(tmp_path: Path) -> None:
    _, app = make_client(tmp_path)
    for index in range(10):
        response = login_from(app, f"198.51.100.{index}", "admin@example.test", "wrong")
        assert response.status_code == 401
    slowed = login_from(app, "198.51.100.200", "admin@example.test", "wrong")
    assert slowed.status_code == 429 and slowed.headers["retry-after"] == "1"

    settings = app.state.settings
    identity = _private_digest(settings, "email:admin@example.test")
    now = datetime.now(UTC)
    with app.state.session_factory.begin() as db:
        db.add_all(
            LoginAttempt(
                email=identity,
                ip_address=_private_digest(settings, f"ip:203.0.113.{index}")[:40],
                succeeded=False,
                attempted_at=now - timedelta(seconds=30 - index),
            )
            for index in range(20)
        )
    capped = login_from(app, "198.51.100.201", "admin@example.test", "long-test-password")
    assert capped.status_code == 429
    assert 50 <= int(capped.headers["retry-after"]) <= 60

    with app.state.session_factory.begin() as db:
        db.execute(update(LoginAttempt).values(attempted_at=now - timedelta(seconds=61)))
    allowed = login_from(app, "198.51.100.201", "admin@example.test", "long-test-password")
    assert allowed.status_code == 200
    app.state.engine.dispose()


def test_wrong_origin_without_allowed_origins_points_to_https_or_a_tunnel(
    tmp_path: Path,
) -> None:
    settings = Settings(
        "Kebivo", "test", "test-secret-key-for-auth-tests-000000", str(tmp_path / "lan.db")
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    lan = "http://192.168.1.20:8000"
    client = TestClient(app, base_url=lan)
    for path, body in (
        ("/api/v1/auth/login", {"email": "a@example.test", "password": "long-test-password"}),
        ("/api/v1/auth/setup", {"token": "x", "email": "a@example.test", "password": "x" * 12}),
    ):
        response = client.post(path, json=body, headers={"Origin": lan})
        assert response.status_code == 403
        message = response.json()["error"]["message"]
        assert "HTTPS reverse proxy" in message and "SSH or VPN tunnel" in message
        assert "127.0.0.1" in message and "docs.kebivo.com/self-hosting" in message
        assert "http:// origin" not in message and "APP_ENV" not in message
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


def test_parallel_wrong_logins_cannot_pass_the_lock_together(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _, app = make_client(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(
            pool.map(
                lambda _: login_from(app, "192.0.2.10", "admin@example.test", "wrong").status_code,
                range(12),
            )
        )
    assert codes.count(401) == 5 and codes.count(429) == 7
    assert (
        login_from(app, "192.0.2.20", "admin@example.test", "long-test-password").status_code == 200
    )
    with app.state.session_factory() as db:
        outcomes = db.scalars(select(LoginAttempt.succeeded)).all()
        assert sorted(outcomes) == [False] * 5 + [True]
    app.state.engine.dispose()
