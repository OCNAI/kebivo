"""Final sweep after every other stage (invariants 10 and 11, AC-15).

scripts/e2e.sh copies the app and runner logs to $E2E_STATE/logs.txt first.
The app volume is mounted read-only at /appdata for the raw byte scan.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from conftest import MODE, STATE, Admin

APPDATA = Path("/appdata")


def _secrets() -> list[dict[str, Any]]:
    lines = (STATE / "state" / "secrets.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _content() -> list[str]:
    lines = (STATE / "state" / "content.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.stage("sweep")
def test_no_credential_or_feedback_text_in_logs() -> None:
    logs = (STATE / "logs.txt").read_text(errors="replace")
    assert len(logs) > 0
    kinds = {item["kind"] for item in _secrets()}
    assert {"admin_password", "session_cookie", "csrf_token", "project_token",
            "webhook_signing_secret", "webhook_bearer", "webhook_header", "smtp_password",
            "llm_api_key", "llm_header"} <= kinds, kinds
    leaked = [item["kind"] for item in _secrets() if item["value"] in logs]
    assert leaked == []
    assert [text for text in _content() if text in logs] == []
    assert "whsec_" not in logs


@pytest.mark.stage("sweep")
def test_no_credential_in_api_responses_after_creation(admin: Admin) -> None:
    bodies = [admin.client.get(path).text for path in (
        "/destinations", "/admin/llm-providers", "/projects", "/admin/settings",
        "/deliveries", "/admin/feedback", "/admin/groups", "/auth/session")]
    for row in admin.get("/destinations")["items"]:
        bodies.append(admin.client.get(f"/destinations/{row['id']}").text)
    for row in admin.get("/admin/llm-providers")["items"]:
        bodies.append(admin.client.get(f"/admin/llm-providers/{row['id']}").text)
    for row in admin.get("/projects")["items"]:
        bodies.append(admin.client.get(f"/projects/{row['id']}").text)
        bodies.append(admin.client.get(f"/projects/{row['id']}/tokens").text)
        bodies.append(admin.client.get(f"/admin/projects/{row['id']}/export").text)
    for row in admin.get("/deliveries")["items"][:50]:
        bodies.append(admin.client.get(f"/deliveries/{row['id']}").text)
    text = "\n".join(bodies)
    leaked = [s["kind"] for s in _secrets() if not s["api_visible"] and s["value"] in text]
    assert leaked == []


@pytest.mark.stage("sweep")
def test_database_files_hold_no_plaintext_credential() -> None:
    files = [path for path in APPDATA.glob("app.sqlite3*") if path.is_file()]
    assert any(path.name == "app.sqlite3" for path in files), files
    raw = b"".join(path.read_bytes() for path in files)
    leaked = [s["kind"] for s in _secrets() if s["value"].encode() in raw]
    assert leaked == []


@pytest.mark.stage("sweep")
def test_ac_15_no_external_lookup_or_connection() -> None:
    queries = [json.loads(line) for line in (STATE / "dns.jsonl").read_text().splitlines()]
    assert queries == [], "the app tried to resolve external names"
    if MODE != "offline":
        pytest.skip("network isolation is checked in the offline run")
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 443)
