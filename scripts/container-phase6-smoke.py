"""Run Phase 6 semantic/provider acceptance against the built Compose image."""

from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import quote

api = os.environ["SMOKE_API"]
origin = os.environ["SMOKE_ORIGIN"]
csrf = os.environ["SMOKE_CSRF"]
cookie_file = os.environ["SMOKE_COOKIE_JAR"]
jar = http.cookiejar.MozillaCookieJar(cookie_file)
jar.load(ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(path: str, body: object | None = None, *, token: str | None = None, method: str | None = None):
    headers = {"Origin": origin, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if method in {"POST", "PATCH", "PUT", "DELETE"}:
        headers["X-CSRF-Token"] = csrf
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers.pop("Origin", None)
        headers.pop("X-CSRF-Token", None)
    req = urllib.request.Request(api + path, data=data, headers=headers, method=method)
    try:
        with (urllib.request.build_opener() if token else opener).open(req, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


def ok(path: str, body: object | None = None, **kwargs):
    status, value = request(path, body, **kwargs)
    assert 200 <= status < 300, (path, status, value)
    return value


def fake_scenario(value: str) -> None:
    req = urllib.request.Request("http://fake-llm:8765/scenario", data=json.dumps({"scenario": value}).encode(), headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=3) as response:
        assert json.load(response)["scenario"] == value


def wait_event(event_id: str, outcome: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect("/data/app.sqlite3") as db:
            row = db.execute("SELECT outcome, matched_group_id, reason FROM dedup_evaluations WHERE event_id=?", (event_id,)).fetchone()
            status = db.execute("SELECT processing_status FROM feedback_events WHERE id=?", (event_id,)).fetchone()
        if row and status and row[0] == outcome and status[0] == "completed":
            return {"outcome": row[0], "matched_group_id": row[1], "reason": row[2]}
        time.sleep(0.2)
    raise AssertionError(f"event {event_id} did not reach {outcome}")


# The sidecar is a private test dependency, and the provider explicitly opts in.
base_url = "http://fake-llm:8765/v1"
provider = ok("/admin/llm-providers", {
    "name": "Phase 6 sidecar provider", "provider_type": "openai_compatible",
    "base_url": base_url, "model": "fake", "allow_private_network": True,
    "timeout_seconds": 2, "structured_output": "prompt_only",
}, method="POST")
provider_id = provider["id"]
project = ok("/projects", {"name": "Phase 6 semantic QA", "slug": "phase6-semantic-qa"}, method="POST")
project_id = project["id"]
token = ok(f"/projects/{project_id}/tokens", {"name": "phase6 token"}, method="POST")["token"]

# A webhook proves fallback routing completes even when model inference fails.
destination = ok("/destinations", {
    "name": "Phase 6 QA receiver", "type": "webhook", "allow_private_network": True,
    "config": {"url": "http://webhook:8080/hook", "signing": True, "auth": {"type": "none"}},
}, method="POST")
ok(f"/projects/{project_id}/routing-rules", {
    "name": "Phase 6 QA route", "event_types": ["knowledge_gap"], "match_mode": "any",
    "tags": ["phase6qa"], "priority": 1, "destination_ids": [destination["id"]], "match_behavior": "stop",
}, method="POST")
changed = ok(f"/projects/{project_id}", {"settings": {"deduplication": {
    "mode": "semantic", "semantic": {"provider_id": provider_id, "min_confidence": 0.9,
    "timeout_seconds": 1, "max_attempts": 1}, "privacy_acknowledged": True,
}}}, method="PATCH")


def submit(question: str, key: str) -> str:
    status, value = request("/feedback", {"event_type": "knowledge_gap", "question": question,
        "tags": ["phase6qa"]}, token=token, method="POST")
    assert status == 202, (status, value)
    return value["event_id"]


def member(event_id: str):
    with sqlite3.connect("/data/app.sqlite3") as db:
        return db.execute("SELECT group_id, match_method FROM feedback_group_members WHERE event_id=?", (event_id,)).fetchone()


# Seed two candidates. The server's c2 response must map to the second offered
# label, and that event's provisional group must be removed.
fake_scenario("valid")
first = submit("VPN password reset access issue", "phase6-first")
fake_scenario("no_match")
second = submit("VPN reset access credentials issue", "phase6-second")
for event in (first, second):
    # These initial events have no candidates, so they complete without inference.
    with sqlite3.connect("/data/app.sqlite3") as db:
        for _ in range(100):
            status = db.execute("SELECT processing_status FROM feedback_events WHERE id=?", (event,)).fetchone()[0]
            if status == "completed": break
            time.sleep(0.1)
third = submit("VPN reset access password issue", "phase6-third")
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    with sqlite3.connect("/data/app.sqlite3") as db:
        state = db.execute("SELECT processing_status FROM feedback_events WHERE id=?", (third,)).fetchone()
    if state and state[0] == "completed":
        break
    time.sleep(0.1)
# Assert the sidecar received and offered two labels before resolving c2.
for _ in range(100):
    with urllib.request.urlopen("http://fake-llm:8765/requests", timeout=3) as response:
        calls = json.load(response)
    if calls: break
    time.sleep(0.1)
assert calls
last_request = json.loads(calls[-1]["body"])
labels = [candidate["label"] for candidate in json.loads(last_request["messages"][-1]["content"])["candidates"]]
assert len(labels) >= 2, labels
# c2 is offered label two; resolve expected target from the second prompt entry.
fake_scenario("valid_c2")
third_retry = submit("VPN reset access password problem", "phase6-c2")
wait_event(third_retry, "matched")
with urllib.request.urlopen("http://fake-llm:8765/requests", timeout=3) as response:
    calls = json.load(response)
last_request = json.loads(calls[-1]["body"])
second_question = json.loads(last_request["messages"][-1]["content"])["candidates"][1]["question"]
with sqlite3.connect("/data/app.sqlite3") as db:
    second_group = db.execute("SELECT m.group_id FROM feedback_group_members m JOIN feedback_events e ON e.id=m.event_id WHERE e.question=? LIMIT 1", (second_question,)).fetchone()[0]
assert member(third_retry) == (second_group, "semantic"), member(third_retry)
with sqlite3.connect("/data/app.sqlite3") as db:
    assert db.execute("SELECT COUNT(*) FROM feedback_groups WHERE id=?", (member(third_retry)[0],)).fetchone()[0] == 1

# Low confidence and malformed/invalid labels leave the event in its own group.
for scenario, expected, question in (
    ("low_confidence", "low_confidence", "VPN reset access password low-confidence"),
    ("malformed", "invalid_response", "VPN reset access password malformed"),
    ("wrong_label", "invalid_candidate", "VPN reset access password invalid-label"),
):
    fake_scenario(scenario)
    event = submit(question, "phase6-" + scenario)
    result = wait_event(event, expected)
    assert result["matched_group_id"] is None
    assert member(event)[1] == "initial"

# AC-10: 202 is durable and fast before slow inference; timeout still routes.
fake_scenario("slow")
started = time.monotonic()
timeout_event = submit("VPN reset access password timeout canary P6-TIMEOUT-6b21", "phase6-timeout")
elapsed = time.monotonic() - started
assert elapsed < 1.0, f"202 waited for model: {elapsed:.2f}s"
wait_event(timeout_event, "timeout", timeout=20)
for _ in range(100):
    with sqlite3.connect("/data/app.sqlite3") as db:
        status = db.execute("SELECT status FROM deliveries WHERE event_id=?", (timeout_event,)).fetchone()
    if status and status[0] == "delivered":
        break
    time.sleep(0.2)
assert status and status[0] == "delivered", status
with urllib.request.urlopen("http://webhook:8080/", timeout=3) as response:
    received = json.load(response)
assert any(json.loads(row["body"]).get("event_id") == timeout_event for row in received)

# Delivery-mode receiver counts: an exact duplicate is suppressed for the
# first destination and delivered again for every_occurrence.
urllib.request.urlopen(urllib.request.Request("http://webhook:8080/", data=b"", method="DELETE"), timeout=3).close()
mode_destinations = {}
for mode in ("new_groups_only", "every_occurrence"):
    row = ok("/destinations", {"name": f"Phase 6 {mode}", "type": "webhook",
        "delivery_mode": mode, "allow_private_network": True,
        "config": {"url": "http://webhook:8080/hook", "signing": True,
        "auth": {"type": "none"}}}, method="POST")
    mode_destinations[mode] = row["id"]
ok(f"/projects/{project_id}/routing-rules", {"name": "Phase 6 delivery-mode rule",
    "event_types": ["knowledge_gap"], "match_mode": "any", "tags": ["deliveryqa"],
    "priority": 1, "destination_ids": list(mode_destinations.values()),
    "match_behavior": "stop"}, method="POST")

def submit_delivery(key: str) -> str:
    status, value = request("/feedback", {"event_type": "knowledge_gap",
        "question": "Delivery mode exact duplicate test", "tags": ["deliveryqa"]},
        token=token, method="POST")
    assert status == 202, (status, value)
    return value["event_id"]

delivery_first = submit_delivery("delivery-mode-first")
delivery_duplicate = submit_delivery("delivery-mode-duplicate")
deadline = time.monotonic() + 20
expected_delivery_ids = set()
while time.monotonic() < deadline:
    with sqlite3.connect("/data/app.sqlite3") as db:
        rows = db.execute("SELECT id,destination_id,status FROM deliveries WHERE destination_id IN (?,?)",
            tuple(mode_destinations.values())).fetchall()
    if all(row[2] == "delivered" for row in rows) and len(rows) == 3:
        expected_delivery_ids = {row[0] for row in rows}
        break
    time.sleep(0.2)
assert len(expected_delivery_ids) == 3, rows
with urllib.request.urlopen("http://webhook:8080/", timeout=3) as response:
    mode_hits = json.load(response)
received_delivery_ids = {item["headers"]["webhook-id"] for item in mode_hits}
assert received_delivery_ids == expected_delivery_ids, (received_delivery_ids, expected_delivery_ids)
counts = {mode: sum(1 for row in rows if row[1] == destination_id)
    for mode, destination_id in mode_destinations.items()}
assert counts == {"new_groups_only": 1, "every_occurrence": 2}, counts

# Azure wire shape and local no-key connection state against the network sidecar.
for kind, extra in (
    ("azure_openai", {"deployment": "fake-deployment", "api_version": "2025-01-01", "api_key": "P6-FAKE-AZURE-KEY"}),
    ("local", {}),
):
    fake_scenario("valid")
    body = {"name": f"Phase6 {kind}", "provider_type": kind, "base_url": base_url,
        "model": "fake", "allow_private_network": True, **extra}
    created = ok("/admin/llm-providers", body, method="POST")
    test_result = ok(f"/admin/llm-providers/{created['id']}/test", method="POST")
    assert test_result["ok"] is True, (kind, test_result)
    with urllib.request.urlopen("http://fake-llm:8765/requests", timeout=3) as response:
        recorded = json.load(response)[-1]
    if kind == "azure_openai":
        assert "/openai/deployments/fake-deployment/chat/completions?api-version=2025-01-01" in recorded["path"]
        assert "api-key" in {name.lower() for name in recorded["headers"]}
    else:
        assert "authorization" not in {name.lower() for name in recorded["headers"]}

# The canary sweep checks semantic prompts/reasons never enter app logs.
print(json.dumps({"semantic_match_c2": "passed", "low_confidence": "passed",
    "malformed_answer": "passed", "invalid_label": "passed", "ac10_fast_202_timeout_delivery": "passed",
    "azure_local_connection": "passed", "delivery_mode_receiver_counts": counts,
    "semantic_prompt_canary": "P6-TIMEOUT-6b21",
    "semantic_reason_canary": "FAKE_REASON_CANARY-D6e8"}))
