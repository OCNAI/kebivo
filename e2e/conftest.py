"""Shared helpers for the Kebivo acceptance suite (run by scripts/e2e.sh).

The suite talks to the built app container only through its public HTTP API
and MCP endpoint. Test doubles run in this runner container (e2e/sidecar.py).
`E2E_STAGE` selects the tests for one step of the host script, so the host can
restart the app between steps; unmarked tests belong to the `main` stage.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest

APP = os.environ.get("E2E_APP_URL", "http://app:8000")
API = APP + "/api/v1"
ORIGIN = os.environ.get("E2E_ORIGIN", "http://127.0.0.1:8000")
STATE = Path(os.environ.get("E2E_STATE", "/tmp/e2e"))
STAGE = os.environ.get("E2E_STAGE", "main")
MODE = os.environ.get("E2E_MODE", "online")
RUNNER = os.environ.get("E2E_RUNNER_HOST", "runner")
RECEIVER = f"http://{RUNNER}:8080"
LLM = f"http://{RUNNER}:8765"
T = TypeVar("T")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "stage(name): run only in this E2E_STAGE step")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    selected, deselected = [], []
    for item in items:
        marker = item.get_closest_marker("stage")
        (selected if (marker.args[0] if marker else "main") == STAGE else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


# -- evidence shared between stages and with the final canary sweep ----------


def save_state(name: str, value: Any) -> None:
    (STATE / "state").mkdir(parents=True, exist_ok=True)
    (STATE / "state" / f"{name}.json").write_text(json.dumps(value))


def load_state(name: str) -> Any:
    return json.loads((STATE / "state" / f"{name}.json").read_text())


def record_secret(kind: str, value: str, *, api_visible: bool = False) -> None:
    """Remember a credential; the sweep checks logs, API responses and the DB."""
    (STATE / "state").mkdir(parents=True, exist_ok=True)
    with (STATE / "state" / "secrets.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, "value": value, "api_visible": api_visible}) + "\n")


def record_content(value: str) -> str:
    """Remember submitted text that must never appear in container logs."""
    (STATE / "state").mkdir(parents=True, exist_ok=True)
    with (STATE / "state" / "content.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\n")
    return value


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def wait_for(
    probe: Callable[[], T | None], *, timeout: float = 30, interval: float = 0.25, what: str = ""
) -> T:
    deadline = time.monotonic() + timeout
    last: T | None = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what or probe}; last={last!r}")


# -- HTTP clients -------------------------------------------------------------


class Admin:
    """A logged-in admin browser session: cookie, Origin and CSRF header."""

    def __init__(self, email: str, password: str) -> None:
        self.client = httpx.Client(base_url=API, headers={"Origin": ORIGIN}, timeout=15)
        response = self.client.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        record_secret("session_cookie", next(iter(self.client.cookies.values())))
        # The SPA reads its CSRF token from /auth/session by design.
        record_secret("csrf_token", self.csrf, api_visible=True)

    def request(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self.client.request(method, path, json=body)

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        response = self.request(method, path, body)
        status = response.status_code
        assert 200 <= status < 300, (method, path, status, response.text)
        return response.json() if response.content else None

    def get(self, path: str) -> Any:
        return self.ok("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.ok("POST", path, body)

    def patch(self, path: str, body: Any) -> Any:
        return self.ok("PATCH", path, body)

    def put(self, path: str, body: Any) -> Any:
        return self.ok("PUT", path, body)

    # -- domain helpers --

    def project(self, name: str | None = None, **changes: Any) -> dict[str, Any]:
        label = name or unique("E2E project")
        slug = unique("e2e")
        project = self.post("/projects", {"name": label, "slug": slug})
        if changes:
            project = self.patch(f"/projects/{project['id']}", changes)
        return dict(project)

    def token(self, project_id: str, scopes: list[str] | None = None) -> str:
        body: dict[str, Any] = {"name": unique("e2e token")}
        if scopes:
            body["scopes"] = scopes
        token = str(self.post(f"/projects/{project_id}/tokens", body)["token"])
        record_secret("project_token", token)
        return token

    def webhook(self, path: str, *, signing: bool = True, **extra: Any) -> dict[str, Any]:
        config = {"url": f"{RECEIVER}/{path}", "signing": signing, "auth": {"type": "none"}}
        config.update(extra.pop("config", {}))
        destination = self.post(
            "/destinations",
            {"name": unique("e2e webhook"), "type": "webhook", "allow_private_network": True,
             "config": config, **extra},
        )
        if destination.get("signing_secret"):
            record_secret("webhook_signing_secret", destination["signing_secret"])
        return dict(destination)

    def smtp(self, **config: Any) -> dict[str, Any]:
        body = {"host": RUNNER, "port": 2525, "tls_mode": "none",
                "from": "kebivo@example.invalid", "to": ["team@example.invalid"], **config}
        return dict(self.post("/destinations", {"name": unique("e2e smtp"), "type": "smtp",
                                                "allow_private_network": True, "config": body}))

    def rule(self, project_id: str, tags: list[str], destinations: list[str], *,
             priority: int = 10, behavior: str = "stop", event_types: list[str] | None = None,
             match_mode: str = "any") -> dict[str, Any]:
        return dict(self.post(f"/projects/{project_id}/routing-rules", {
            "name": unique("e2e rule"), "event_types": event_types or ["knowledge_gap"],
            "match_mode": match_mode, "tags": tags, "priority": priority,
            "destination_ids": destinations, "match_behavior": behavior,
        }))

    def event(self, event_id: str) -> dict[str, Any]:
        return dict(self.get(f"/admin/feedback/{event_id}"))

    def processed(self, event_id: str, timeout: float = 30) -> dict[str, Any]:
        def probe() -> dict[str, Any] | None:
            detail = self.event(event_id)
            return detail if detail["system"]["processing_status"] == "completed" else None

        return wait_for(probe, timeout=timeout, what=f"event {event_id} processed")

    def deliveries(self, event_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"/deliveries?event_id={event_id}")["items"])

    def delivered(self, event_id: str, *, count: int = 1, status: str = "delivered",
                  timeout: float = 40) -> list[dict[str, Any]]:
        def probe() -> list[dict[str, Any]] | None:
            rows = self.deliveries(event_id)
            return rows if len(rows) == count and all(r["status"] == status for r in rows) else None

        return wait_for(probe, timeout=timeout, what=f"{count} {status} deliveries for {event_id}")

    def group_of(self, event_id: str) -> dict[str, Any]:
        group = self.event(event_id)["group"]
        assert group is not None
        return dict(self.get(f"/admin/groups/{group['id']}"))

    def group_events(self, group_id: str) -> list[dict[str, Any]]:
        return list(self.get(f"/admin/groups/{group_id}/events")["items"])


@pytest.fixture(scope="session")
def admin() -> Admin:
    return Admin(os.environ["E2E_ADMIN_EMAIL"], os.environ["E2E_ADMIN_PASSWORD"])


def submit(token: str, body: dict[str, Any], key: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return httpx.post(f"{API}/feedback", json=body, headers=headers, timeout=15)


def gap(token: str, question: str, tags: list[str] | None = None, key: str | None = None) -> str:
    response = submit(token, {"event_type": "knowledge_gap", "question": question,
                              "tags": tags or []}, key)
    assert response.status_code == 202, response.text
    return str(response.json()["event_id"])


# -- test doubles in the runner -------------------------------------------------


class Receiver:
    def reset(self, fail_first: int = 0, hold_seconds: float = 0) -> None:
        httpx.request("DELETE", RECEIVER + "/", headers={
            "X-Fail-First": str(fail_first), "X-Hold-Seconds": str(hold_seconds)}, timeout=5)

    def hits(self, path: str | None = None) -> list[dict[str, Any]]:
        rows = httpx.get(RECEIVER + "/", timeout=5).json()
        return [row for row in rows if path is None or row["path"] == "/" + path]


class Mail:
    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text()) for path in sorted((STATE / "mail").glob("*.json"))]

    def matching(self, text: str) -> list[dict[str, Any]]:
        return [message for message in self.messages() if text in message["data"]]

    def down(self, value: bool) -> None:
        flag = STATE / "smtp-down"
        if value:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)


class FakeLlm:
    def scenario(self, name: str) -> None:
        response = httpx.put(LLM + "/scenario", json={"scenario": name}, timeout=5)
        assert response.status_code == 200, response.text

    def requests(self) -> list[dict[str, Any]]:
        return list(httpx.get(LLM + "/requests", timeout=5).json())


@pytest.fixture(scope="session")
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture(scope="session")
def mail() -> Mail:
    return Mail()


@pytest.fixture(scope="session")
def llm() -> FakeLlm:
    return FakeLlm()


@asynccontextmanager
async def mcp_session(token: str) -> AsyncIterator[Any]:
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(
            APP + "/mcp", http_client=http, terminate_on_close=False
        ) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.discover()
                yield session


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)
