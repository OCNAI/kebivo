#!/usr/bin/env python3
"""API-only fixture and checks for upgrade-check.sh (stdlib only)."""

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = os.environ["UPGRADE_URL"]
STATE = Path(os.environ["UPGRADE_STATE"])
ORIGIN = "http://127.0.0.1:18099"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
csrf = ""


def call(method: str, path: str, body: dict | None = None, token: str = "") -> dict:
    headers = {"Origin": ORIGIN}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if csrf and method != "GET":
        headers["X-CSRF-Token"] = csrf
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=15) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {exc.read()[:500]!r}") from exc


def login() -> None:
    global csrf
    result = call("POST", "/api/v1/auth/login", {
        "email": "upgrade-admin@example.invalid", "password": os.environ["UPGRADE_PASSWORD"]
    })
    csrf = result["csrf_token"]


def seed() -> None:
    login()
    project = call("POST", "/api/v1/projects", {
        "name": "Upgrade sample", "slug": "upgrade-sample"
    })
    project_id = project["id"]
    token = call("POST", f"/api/v1/projects/{project_id}/tokens", {
        "name": "upgrade token"
    })["token"]
    call("PUT", f"/api/v1/projects/{project_id}/default-tags", {"tags": ["upgrade"]})
    destination = call("POST", "/api/v1/destinations", {
        "name": "Upgrade webhook", "type": "webhook", "enabled": True,
        "allow_private_network": True,
        "config": {"url": "http://127.0.0.1:1/hook", "signing": True,
                   "auth": {"type": "bearer", "token": "upgrade-destination-secret"}}
    })
    call("POST", f"/api/v1/projects/{project_id}/routing-rules", {
        "name": "Upgrade route", "event_types": ["knowledge_gap"],
        "match_mode": "any", "tags": ["upgrade"], "priority": 1,
        "destination_ids": [destination["id"]], "match_behavior": "stop"
    })
    provider = call("POST", "/api/v1/admin/llm-providers", {
        "name": "Upgrade local provider", "provider_type": "local",
        "base_url": "http://127.0.0.1:1/v1", "model": "upgrade-model",
        "structured_output": "json_object", "enabled": False,
        "allow_private_network": True, "api_key": "upgrade-provider-secret"
    })
    questions = ["Upgrade shared question", "UPGRADE SHARED QUESTION", "Upgrade other group"]
    ids = []
    for index, question in enumerate(questions):
        result = call("POST", "/api/v1/feedback", {
            "event_type": "knowledge_gap", "question": question,
            "tags": ["upgrade"], "source_event_id": f"upgrade-{index}"
        }, token)
        ids.append(result["event_id"])
    groups = [call("GET", f"/api/v1/admin/feedback/{event_id}")["group"]["id"] for event_id in ids]
    assert groups[0] == groups[1] and groups[0] != groups[2], groups
    call("POST", f"/api/v1/admin/groups/{groups[0]}/notes", {
        "body": "Upgrade note survives migration"
    })
    call("POST", f"/api/v1/admin/groups/{groups[0]}/merge", {
        "source_group_ids": [groups[2]]
    })
    call("POST", f"/api/v1/admin/groups/{groups[0]}/split", {
        "event_ids": [ids[1]]
    })
    # The worker is disabled during the fixture to preserve pending jobs. The
    # migration must keep those jobs and their credentials intact.
    STATE.write_text(json.dumps({
        "project_id": project_id, "token": token, "event_ids": ids,
        "destination_id": destination["id"], "provider_id": provider["id"]
    }))
    print(json.dumps({"seeded_events": len(ids), "project_id": project_id}))


def verify() -> None:
    state = json.loads(STATE.read_text())
    login()
    project_id = state["project_id"]
    project = call("GET", f"/api/v1/projects/{project_id}")
    assert project["id"] == project_id
    destination = call("GET", f"/api/v1/destinations/{state['destination_id']}")
    assert destination["secrets_unavailable"] is False
    provider = call("GET", f"/api/v1/admin/llm-providers/{state['provider_id']}")
    assert provider["api_key"] == {"configured": True, "source": "stored"}
    for event_id in state["event_ids"]:
        event = call("GET", f"/api/v1/admin/feedback/{event_id}")
        assert event["id"] == event_id
    result = call("POST", "/api/v1/feedback", {
        "event_type": "knowledge_gap", "question": "Upgrade token still works",
        "tags": ["upgrade"]
    }, state["token"])
    assert result["status"] == "accepted"
    print(json.dumps({"upgrade_api": "passed", "old_token": "accepted"}))


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    elif sys.argv[1] == "verify":
        verify()
    else:
        raise SystemExit("Expected seed or verify")
