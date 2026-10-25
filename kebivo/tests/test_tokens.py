"""Project-token disclosure, isolation, lifecycle, and scope contract tests."""

import json
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from starlette.requests import Request

from kebivo.models import ApiToken
from kebivo.tests.test_projects import ORIGIN, make_client, write_headers
from kebivo.tokens import (
    CORRECTION_SCOPE,
    GAP_SCOPE,
    ProjectTokenPrincipal,
    require_project_token,
)


def verify(app: FastAPI, token: str, scope: str = GAP_SCOPE) -> ProjectTokenPrincipal:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "app": app,
        }
    )
    request.state.db = app.state.session_factory()
    try:
        return require_project_token(scope)(request)
    finally:
        request.state.db.close()


def rejected(app: FastAPI, token: str, status: int, scope: str = GAP_SCOPE) -> None:
    try:
        verify(app, token, scope)
    except HTTPException as exc:
        assert exc.status_code == status
    else:
        raise AssertionError(f"Expected project token rejection with status {status}")


def create_project(client: TestClient, slug: str, *, corrections: bool = False) -> dict[str, Any]:
    response = client.post(
        "/api/v1/projects",
        json={
            "name": slug.title(),
            "slug": slug,
            "settings": {
                "corrections": {"enabled": corrections},
                "deduplication": {"enabled": False},
            },
        },
        headers=write_headers(client),
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def issue_token(
    client: TestClient, project_id: str, name: str, scopes: list[str] | None = None
) -> Response:
    payload: dict[str, Any] = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    response = client.post(
        f"/api/v1/projects/{project_id}/tokens", json=payload, headers=write_headers(client)
    )
    return response


def test_token_is_disclosed_once_and_rotation_revokes_old_credential(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "tokens.db")
    project = create_project(client, "first")
    created = issue_token(client, project["id"], "automation")
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    bearer = body["token"]
    assert body["scopes"] == [GAP_SCOPE]
    listed = client.get(f"/api/v1/projects/{project['id']}/tokens").json()["items"]
    assert listed[0].get("token") is None
    assert verify(app, bearer).project_id == project["id"]
    with app.state.session_factory() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.id == body["id"]))
        assert row is not None
        assert bearer not in row.token_hash and row.token_hash != bearer
        assert json.loads(row.scopes_json) == [GAP_SCOPE]

    replacement = issue_token(client, project["id"], "automation rotation").json()
    assert replacement["token"] != bearer
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/tokens/{body['id']}/revoke",
            headers=write_headers(client),
        ).status_code
        == 200
    )
    rejected(app, bearer, 401)
    assert verify(app, replacement["token"]).project_id == project["id"]
    # Admin routes require the cookie session and do not accept bearer tokens.
    assert (
        TestClient(app, base_url=ORIGIN)
        .get("/api/v1/projects", headers={"Authorization": f"Bearer {replacement['token']}"})
        .status_code
        == 401
    )
    app.state.engine.dispose()


def test_project_isolation_correction_double_gate_and_disabled_projects(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "scopes.db")
    first = create_project(client, "first", corrections=True)
    second = create_project(client, "second")
    default_token = issue_token(client, first["id"], "gap only").json()["token"]
    rejected(app, default_token, 403, CORRECTION_SCOPE)
    pending_correction = issue_token(
        client, second["id"], "not yet enabled", [GAP_SCOPE, CORRECTION_SCOPE]
    )
    assert pending_correction.status_code == 201
    rejected(app, pending_correction.json()["token"], 403, CORRECTION_SCOPE)
    enabled = client.patch(
        f"/api/v1/projects/{second['id']}",
        json={"settings": {"corrections": {"enabled": True}, "deduplication": {"enabled": False}}},
        headers=write_headers(client),
    )
    assert enabled.status_code == 200
    correction_result = issue_token(
        client, second["id"], "corrector", [GAP_SCOPE, CORRECTION_SCOPE]
    ).json()
    correction_token = correction_result["token"]
    first_token = client.get(f"/api/v1/projects/{first['id']}/tokens").json()["items"][0]
    second_token = client.get(f"/api/v1/projects/{second['id']}/tokens").json()["items"]
    assert all(token["id"] != first_token["id"] for token in second_token)
    wrong_project_revoke = client.post(
        f"/api/v1/projects/{first['id']}/tokens/{correction_result['id']}/revoke",
        headers=write_headers(client),
    )
    assert wrong_project_revoke.status_code == 404
    assert verify(app, correction_token).project_id == second["id"]
    assert verify(app, correction_token, CORRECTION_SCOPE).project_id == second["id"]

    # Tokens derive their project from the DB credential, never request data;
    # a credential from project two cannot be used as project one's route auth.
    assert verify(app, correction_token).project_id != first["id"]
    client.patch(
        f"/api/v1/projects/{second['id']}", json={"is_active": False}, headers=write_headers(client)
    )
    rejected(app, correction_token, 401)
    app.state.engine.dispose()


def test_token_requires_explicit_supported_scopes_and_expiry(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "expiry.db")
    project = create_project(client, "expires")
    assert issue_token(client, project["id"], "bad", [CORRECTION_SCOPE]).status_code == 422
    unknown_scope = issue_token(client, project["id"], "unknown", [GAP_SCOPE, "admin:write"])
    assert unknown_scope.status_code == 422
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/tokens",
            json={"name": "past", "expires_at": "2020-01-01T00:00:00Z"},
            headers=write_headers(client),
        ).status_code
        == 422
    )
    token = client.post(
        f"/api/v1/projects/{project['id']}/tokens",
        json={"name": "expired", "expires_at": "2099-01-01T00:00:00Z"},
        headers=write_headers(client),
    ).json()
    with app.state.session_factory.begin() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.id == token["id"]))
        row.expires_at = row.created_at.replace(year=2000)
    rejected(app, token["token"], 401)
    app.state.engine.dispose()
