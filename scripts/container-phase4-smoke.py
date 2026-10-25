"""Exercise core Phase 4 paths against the built container."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request

origin = os.environ["SMOKE_ORIGIN"]
api = os.environ["SMOKE_API"]
jar = http.cookiejar.MozillaCookieJar(os.environ["SMOKE_COOKIE_JAR"])
jar.load(ignore_discard=True, ignore_expires=True)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf = os.environ["SMOKE_CSRF"]


def request(path: str, body: object | None = None, *, token: str | None = None, method: str | None = None, idempotency_key: str | None = None):
    headers = {"Origin": origin, "Accept": "application/json"}
    data = None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        headers["X-CSRF-Token"] = csrf
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers.pop("X-CSRF-Token", None)
        headers.pop("Origin", None)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
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


def create_destination(name: str, kind: str, config: dict, private: bool = False):
    return ok("/destinations", {"name": name, "type": kind, "config": config, "allow_private_network": private}, method="POST")


# The project token is not an admin credential. Check all three protected APIs.
project = ok("/projects", {"name": "Phase 4 container smoke", "slug": "phase4-container-smoke"}, method="POST")
pid = project["id"]
_, token_result = request(f"/projects/{pid}/tokens", {"name": "phase4 smoke"}, method="POST")
assert token_result, token_result
token = token_result["token"]
for protected, method, body in (("/destinations", "GET", None), ("/deliveries", "GET", None), (f"/projects/{pid}/routing/simulate", "POST", {"event_type": "knowledge_gap"})):
    status, _ = request(protected, body, token=token, method=method)
    assert status == 401, (protected, status)
ok(f"/projects/{pid}/default-tags", {"tags": ["customer-service"]}, method="PUT")

# The metadata service address stays blocked even after the administrator opts in.
status, _ = request("/destinations", {
    "name": "blocked metadata smoke", "type": "webhook", "allow_private_network": True,
    "config": {"url": "http://169.254.169.254/secret-canary-path", "signing": False},
}, method="POST")
assert status == 422, ("metadata address was accepted", status)

smtp = create_destination("Phase4 Mailpit", "smtp", {
    "host": "mailpit", "port": 1025, "tls_mode": "none",
    "from": "smoke@example.invalid", "to": ["qa@example.invalid"],
}, True)
hook = create_destination("Phase4 signed webhook", "webhook", {
    "url": "http://webhook:8080/hook", "signing": True, "auth": {"type": "none"},
}, True)
assert hook["allow_private_network"] is True
secret = hook["signing_secret"]

# Security redaction spot checks for every destination secret class. Signing
# material is shown once at creation by design; subsequent API and audit views
# must not reveal it or any configured canary.
canaries = {
    "path": "smoke-url-path-canary-73d2",
    "token": "smoke-token-canary-48a1",
    "header": "smoke-header-canary-2c19",
    "smtp": "smoke-smtp-password-canary-b419",
}
redacted_hook = create_destination("Phase4 redaction webhook", "webhook", {
    "url": f"http://webhook:8080/{canaries['path']}", "signing": True,
    "auth": {"type": "bearer", "token": canaries["token"]},
    "custom_headers": {"X-Smoke-Canary": canaries["header"]},
}, True)
redacted_smtp = create_destination("Phase4 redaction SMTP", "smtp", {
    "host": "mailpit", "port": 1025, "tls_mode": "starttls",
    "username": "smoke-smtp-user", "password": canaries["smtp"],
    "from": "smoke@example.invalid", "to": ["qa@example.invalid"],
}, True)
for identifier in (redacted_hook["id"], redacted_smtp["id"]):
    response = json.dumps(ok(f"/destinations/{identifier}"))
    listing = json.dumps(ok("/destinations"))
    for value in (*canaries.values(), secret, redacted_hook["signing_secret"]):
        assert value not in response and value not in listing, (identifier, value)
with __import__("sqlite3").connect("/data/app.sqlite3") as connection:
    audit_rows = connection.execute(
        "SELECT details_json FROM audit_events WHERE subject_id IN (?, ?)",
        (redacted_hook["id"], redacted_smtp["id"]),
    ).fetchall()
audit_text = json.dumps(audit_rows)
assert all(value not in audit_text for value in (*canaries.values(), secret, redacted_hook["signing_secret"]))
assert "whsec_" not in audit_text

def rule(name: str, tag: str, destination_id: str, priority: int):
    return ok(f"/projects/{pid}/routing-rules", {
        "name": name, "event_types": ["knowledge_gap"], "match_mode": "any",
        "tags": [tag], "priority": priority, "destination_ids": [destination_id],
        "match_behavior": "stop",
    }, method="POST")

rule("SMTP customer service", "customer-service", smtp["id"], 10)
rule("Signed webhook IT", "it", hook["id"], 20)
before_simulate = ok("/deliveries?project_id=" + pid)["items"]
with urllib.request.urlopen("http://webhook:8080/", timeout=3) as response:
    hits_before_simulate = json.load(response)
simulation = ok(f"/projects/{pid}/routing/simulate", {"event_type": "knowledge_gap", "tags": ["customer-service"]}, method="POST")
assert any(item["destination_id"] == smtp["id"] for item in simulation["destinations"]), simulation
assert ok("/deliveries?project_id=" + pid)["items"] == before_simulate
with urllib.request.urlopen("http://webhook:8080/", timeout=3) as response:
    assert json.load(response) == hits_before_simulate

def submit(question: str, tags: list[str], key: str) -> str:
    status, value = request("/feedback", {"event_type": "knowledge_gap", "question": question, "tags": tags}, token=token, method="POST", idempotency_key=key)
    assert status == 202, (status, value)
    return value["event_id"]


def delivery_for(event_id: str, *, status: str = "delivered", timeout: int = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = ok(f"/deliveries?project_id={pid}")["items"]
        found = next((row for row in rows if row["event_id"] == event_id), None)
        if found and found["status"] == status:
            return ok(f"/deliveries/{found['id']}")
        time.sleep(0.25)
    raise AssertionError(f"delivery did not reach {status}: {event_id}")

smtp_event = submit("Phase4 SMTP evidence", ["customer-service"], "phase4-smtp")
for _ in range(40):
    current = ok(f"/deliveries?project_id={pid}")["items"]
    if any(row["event_id"] == smtp_event and row["status"] == "delivered" for row in current):
        break
    time.sleep(0.5)
else:
    raise AssertionError(f"SMTP delivery did not complete: {current}")
# Isolate the ANY(it) rule after proving the default-tag SMTP route.
ok(f"/projects/{pid}/default-tags", {"tags": []}, method="PUT")
hook_event = submit("Phase4 webhook evidence", ["it"], "phase4-webhook")

deliveries = None
for _ in range(40):
    deliveries = ok(f"/deliveries?project_id={pid}")["items"]
    if len(deliveries) == 2 and all(row["status"] == "delivered" for row in deliveries):
        break
    time.sleep(0.5)
assert deliveries is not None and len(deliveries) == 2, deliveries
assert all(row["status"] == "delivered" for row in deliveries), deliveries
by_event = {row["event_id"]: row for row in deliveries}
assert set(by_event) == {smtp_event, hook_event}
hook_delivery = by_event[hook_event]["id"]

with urllib.request.urlopen("http://webhook:8080/", timeout=3) as response:
    received = json.load(response)
assert len(received) == 1, received
item = received[0]
headers = item["headers"]
raw = item["body"].encode()
payload = json.loads(raw)
assert payload["delivery_id"] == hook_delivery == headers["webhook-id"]
assert headers["x-delivery-id"] == hook_delivery
key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + raw
signature = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
assert hmac.compare_digest(signature, headers["webhook-signature"])
with open("/tmp/webhook-payload.schema.json", encoding="utf-8") as stream:
    schema = json.load(stream)
try:
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate(payload)
except ImportError:
    assert set(payload) == set(schema["required"])
assert payload["test"] is False and payload["feedback"]["question"] == "Phase4 webhook evidence"

with urllib.request.urlopen("http://mailpit:8025/api/v1/messages", timeout=3) as response:
    mail = json.load(response)
assert mail["total"] == 1, mail
message_id = mail["messages"][0]["ID"]
with urllib.request.urlopen(f"http://mailpit:8025/api/v1/message/{message_id}", timeout=3) as response:
    message = json.load(response)
assert message["Subject"] == "[Knowledge Gap] Phase 4 container smoke — Phase4 SMTP evidence", message["Subject"]
for field in ("Phase4 SMTP evidence", "Phase 4 container smoke", "customer-service"):
    assert field in message.get("Text", ""), message

# AC-04: continuing rules may plan the same destination twice, but create one
# delivery and retain the duplicate decision in the routing audit.
second_hook = create_destination("Phase4 second webhook", "webhook", {
    "url": "http://webhook:8080/hook", "signing": False, "auth": {"type": "none"},
}, True)
rule("Continue webhook first", "multi-route", hook["id"], 30)
rule("Continue second destination", "multi-route", second_hook["id"], 31)
rule("Continue duplicate webhook", "multi-route", hook["id"], 32)
for duplicate_rule in ok(f"/projects/{pid}/routing-rules")["items"]:
    if duplicate_rule["name"].startswith("Continue "):
        ok(f"/projects/{pid}/routing-rules/{duplicate_rule['id']}", {"match_behavior": "continue"}, method="PATCH")
dup_event = submit("Phase4 duplicate evidence", ["multi-route"], "phase4-duplicate")
dup_delivery = delivery_for(dup_event)
assert len(dup_delivery["attempts"]) == 1
dup_rows = ok(f"/deliveries?project_id={pid}&event_id={dup_event}")["items"]
assert len(dup_rows) == 2 and all(row["status"] == "delivered" for row in dup_rows), dup_rows
matches = ok(f"/admin/feedback/{dup_event}")["routing"]["matches"]
assert sum(1 for match in matches if match["outcome"] == "duplicate_destination") == 1, matches
rule("Stop first destination", "stop-variant", hook["id"], 50)
rule("Stop prevents later destination", "stop-variant", second_hook["id"], 51)
stop_event = submit("Phase4 stop evidence", ["stop-variant"], "phase4-stop")
stop_rows = None
for _ in range(40):
    stop_rows = ok(f"/deliveries?project_id={pid}&event_id={stop_event}")["items"]
    if stop_rows and stop_rows[0]["status"] == "delivered":
        break
    time.sleep(0.25)
assert stop_rows and len(stop_rows) == 1 and stop_rows[0]["destination_id"] == hook["id"], stop_rows

# AC-05: a fallback delivers on a no-match event; with no fallback, processing
# completes and the event remains visible in the inbox without a delivery.
ok(f"/projects/{pid}", {"fallback_destination_id": smtp["id"]}, method="PATCH")
fallback_event = submit("Phase4 fallback evidence", ["unmatched-fallback"], "phase4-fallback")
fallback_delivery = delivery_for(fallback_event)
assert fallback_delivery["destination_id"] == smtp["id"]
ok(f"/projects/{pid}", {"fallback_destination_id": None}, method="PATCH")
no_fallback_event = submit("Phase4 no fallback evidence", ["unmatched-none"], "phase4-no-fallback")
for _ in range(40):
    inbox = ok(f"/admin/feedback/{no_fallback_event}")
    if inbox["system"]["processing_status"] == "completed":
        break
    time.sleep(0.25)
assert inbox["system"]["processing_status"] == "completed", inbox
assert not any(row["event_id"] == no_fallback_event for row in ok(f"/deliveries?project_id={pid}")["items"])

# AC-11: 500, 500, 200 is retryable and then succeeds; six failures terminate
# the automatic retry budget, after which one manual attempt can recover it.
def configure_receiver(fail_first: int) -> None:
    req = urllib.request.Request("http://webhook:8080/", data=b"", method="DELETE", headers={"X-Fail-First": str(fail_first)})
    with urllib.request.urlopen(req, timeout=3) as response:
        assert response.status == 204

rule("Retryable webhook", "retryable", hook["id"], 40)
configure_receiver(2)
retry_event = submit("Phase4 retry success", ["retryable"], "phase4-retry-success")
retry_delivery = delivery_for(retry_event, timeout=30)
assert [attempt["result"] for attempt in retry_delivery["attempts"]] == ["retryable_failure", "retryable_failure", "success"], retry_delivery
configure_receiver(6)
failed_event = submit("Phase4 terminal retry", ["retryable"], "phase4-retry-terminal")
failed_delivery = delivery_for(failed_event, status="failed", timeout=40)
assert len(failed_delivery["attempts"]) == 6
configure_receiver(6)
manual_event = submit("Phase4 manual retry", ["retryable"], "phase4-manual-retry")
manual_failed = delivery_for(manual_event, status="failed", timeout=40)
configure_receiver(0)
manual = ok(f"/deliveries/{manual_failed['id']}/retry", method="POST")
assert manual["status"] == "delivered" and manual["attempts"][-1]["trigger"] == "manual", manual

print(json.dumps({"ac02_smtp": "passed", "ac03_signed_webhook": "passed", "ac04_continue_duplicate_and_stop": "passed", "ac05_fallback_and_no_fallback": "passed", "ac11_retry_and_manual_retry": "passed", "security_redaction_api_audit": "passed", "simulate": "passed", "allow_private_network_disclosed": True, "delivery_ids": [row["id"] for row in deliveries]}))
