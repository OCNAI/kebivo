from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import select

from kebivo.app import create_app
from kebivo.auth import COOKIE_NAME, create_admin
from kebivo.models import (
    AdminUser,
    AuditEvent,
    Base,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroupMember,
    ProcessingJob,
    Tag,
)
from kebivo.settings import Settings

ORIGIN = "https://admin.example.test"


def make_client(path: Path) -> tuple[TestClient, FastAPI]:
    settings = Settings(
        "Kebivo",
        "test",
        "project-tests-secret-key-000000000000",
        str(path),
        allowed_origins=(ORIGIN,),
    )
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        if db.scalar(select(AdminUser).where(AdminUser.email == "admin@example.test")) is None:
            create_admin(db, "admin@example.test", "long-test-password")
    client = TestClient(app, base_url=ORIGIN)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.test", "password": "long-test-password"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    assert client.cookies.get(COOKIE_NAME)
    return client, app


def write_headers(client: TestClient) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": client.get("/api/v1/auth/session").json()["csrf_token"],
    }


def test_project_settings_tags_lifecycle_and_restart_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "projects.db"
    client, app = make_client(db_path)
    create = client.post(
        "/api/v1/projects",
        json={"name": "Research", "slug": "research"},
        headers=write_headers(client),
    )
    assert create.status_code == 201
    project = create.json()
    assert project["settings"] == {
        "corrections": {"enabled": False},
        "deduplication": {"mode": "exact"},
    }
    assert project["tags"] == []

    changed = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"description": "Persisted", "settings": {"corrections": {"enabled": True}}},
        headers=write_headers(client),
    )
    assert changed.status_code == 200
    assert changed.json()["settings"] == {
        "corrections": {"enabled": True},
        "deduplication": {"mode": "exact"},
    }
    duplicate = client.post(
        "/api/v1/projects",
        json={"name": "Duplicate", "slug": "research"},
        headers=write_headers(client),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "slug_conflict"

    disabled = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"is_active": False},
        headers=write_headers(client),
    )
    assert disabled.status_code == 200 and disabled.json()["is_active"] is False
    archived = client.post(
        f"/api/v1/projects/{project['id']}/archive", headers=write_headers(client)
    )
    assert archived.status_code == 200 and archived.json()["archived_at"]
    assert client.get("/api/v1/projects").json()["items"] == []
    assert len(client.get("/api/v1/projects?include_archived=true").json()["items"]) == 1

    app.state.engine.dispose()
    client2, app2 = make_client(db_path)
    persisted = client2.get(f"/api/v1/projects/{project['id']}")
    assert persisted.status_code == 200
    assert persisted.json()["description"] == "Persisted"
    assert persisted.json()["archived_at"]
    restored = client2.post(
        f"/api/v1/projects/{project['id']}/restore", headers=write_headers(client2)
    )
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    assert restored.json()["is_active"] is False
    app2.state.engine.dispose()


def test_projects_require_admin_and_csrf_for_writes(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "auth.db")
    payload = {"name": "Private", "slug": "private"}
    assert client.post("/api/v1/projects", json=payload).status_code == 403
    assert (
        client.post(
            "/api/v1/projects", json=payload, headers={"Origin": "https://evil.example.test"}
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/v1/projects").status_code == 401
    app.state.engine.dispose()


def test_admin_test_event_uses_ingest_and_limits_writes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("kebivo.feedback_limits.time", SimpleNamespace(monotonic=lambda: 120.0))
    client, app = make_client(tmp_path / "test-event.db")
    project = client.post(
        "/api/v1/projects",
        json={"name": "Research", "slug": "research"},
        headers=write_headers(client),
    ).json()
    path = f"/api/v1/projects/{project['id']}/test-event"
    assert client.post(path).status_code == 403
    assert (
        client.post(
            path, headers={**write_headers(client), "Authorization": "Bearer bad"}
        ).status_code
        == 401
    )
    headers = write_headers(client)
    first = client.post(path, headers=headers)
    second = client.post(path, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["event_id"] != second.json()["event_id"]
    assert first.json()["group_id"] == second.json()["group_id"]
    with app.state.session_factory() as db:
        events = db.scalars(select(FeedbackEvent).order_by(FeedbackEvent.created_at)).all()
        assert len(events) == 2
        assert all(event.api_token_id is None and event.submitted_via == "rest" for event in events)
        assert len(db.scalars(select(ProcessingJob)).all()) == 2
        assert len(db.scalars(select(FeedbackGroupMember)).all()) == 2
        tags = db.scalars(select(Tag.slug).join(FeedbackEventTag)).all()
        assert tags == ["kebivo-test", "kebivo-test"]
        audits = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "project.test_event_sent")
        ).all()
        assert [audit.subject_id for audit in audits] == [project["id"], project["id"]]
    for _ in range(8):
        assert client.post(path, headers=headers).status_code == 202
    limited = client.post(path, headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    client.post(f"/api/v1/projects/{project['id']}/archive", headers=headers)
    archived = client.post(path, headers=headers)
    assert archived.status_code == 409
    app.state.engine.dispose()


def test_project_list_uses_cursor_pagination(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "pages.db")
    for slug in ("one", "two", "three"):
        response = client.post(
            "/api/v1/projects",
            json={"name": slug.title(), "slug": slug},
            headers=write_headers(client),
        )
        assert response.status_code == 201
    first = client.get("/api/v1/projects?limit=2").json()
    second = client.get(f"/api/v1/projects?limit=2&cursor={first['next_cursor']}").json()
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["items"][-1]["id"] != second["items"][0]["id"]
    invalid = client.get("/api/v1/projects?cursor=invalid")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_cursor"
    app.state.engine.dispose()


def test_default_tags_replace_normalize_and_audit(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "defaults.db")
    project = client.post(
        "/api/v1/projects", json={"name": "P", "slug": "p"}, headers=write_headers(client)
    ).json()
    path = f"/api/v1/projects/{project['id']}/default-tags"
    assert client.put(path, json={"tags": ["IT"]}).status_code == 403
    assert (
        client.put(
            path, json={"tags": ["IT"]}, headers={"Origin": "https://evil.example.test"}
        ).status_code
        == 403
    )
    changed = client.put(
        path, json={"tags": [" Customer Service ", "IT", "it"]}, headers=write_headers(client)
    )
    assert changed.status_code == 200
    assert [(tag["slug"], tag["is_default"]) for tag in changed.json()["tags"]] == [
        ("customer-service", True),
        ("it", True),
    ]
    with app.state.session_factory() as db:
        audit = db.scalar(
            select(AuditEvent).where(AuditEvent.action == "project.default_tags_updated")
        )
        assert audit is not None
        assert audit.details_json == '{"slugs":["customer-service","it"]}'
    invalid = client.put(path, json={"tags": ["Valid", "bad!"]}, headers=write_headers(client))
    assert invalid.status_code == 422 and "tags[1]" in invalid.json()["error"]["message"]
    replaced = client.put(path, json={"tags": ["IT"]}, headers=write_headers(client))
    assert replaced.status_code == 200
    assert {tag["slug"]: tag["is_default"] for tag in replaced.json()["tags"]} == {
        "customer-service": False,
        "it": True,
    }
    with app.state.session_factory() as db:
        assert len(db.scalars(select(Tag)).all()) == 2
    client.post(f"/api/v1/projects/{project['id']}/archive", headers=write_headers(client))
    archived = client.put(path, json={"tags": []}, headers=write_headers(client))
    assert archived.status_code == 409
    app.state.engine.dispose()
