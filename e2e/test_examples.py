"""Every file in examples/ and every UI integration snippet runs against the app."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from conftest import APP, Admin, mcp_session, record_content, run, unique

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
SNIPPETS = ROOT / "frontend" / "src" / "integrationSnippets.ts"
SAMPLE_QUESTION = "How do I reset my corporate VPN?"


def _run(command: list[str], token: str, url: str = APP, **env: str) -> dict[str, Any]:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=90, check=False,
        env={**os.environ, "KEBIVO_URL": url, "KEBIVO_TOKEN": token, **env},
    )
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return dict(json.loads(result.stdout.strip().splitlines()[-1]))


def _project(admin: Admin) -> tuple[dict[str, Any], str]:
    project = admin.project()
    return project, admin.token(project["id"])


REST_EXAMPLES = {
    "curl": ["bash", str(EXAMPLES / "rest_curl.sh")],
    "python": [sys.executable, str(EXAMPLES / "rest_python.py"), "--question"],
    "typescript": ["node", str(EXAMPLES / "rest_typescript.ts")],
}


def test_rest_examples_submit_and_replay_with_one_idempotency_key(admin: Admin) -> None:
    project, token = _project(admin)
    for name, command in REST_EXAMPLES.items():
        question = record_content(unique(f"Example {name} question"))
        key = unique(f"example-{name}")
        first = _run([*command, question], token, IDEMPOTENCY_KEY=key)
        assert first["status"] == "accepted", first
        detail = admin.processed(first["event_id"])
        assert detail["agent_reported"]["question"] == question
        assert detail["system"]["idempotency_key"] == key
        again = _run([*command, question], token, IDEMPOTENCY_KEY=key)
        assert again["event_id"] == first["event_id"], name
    listed = admin.get(f"/admin/feedback?project_id={project['id']}")["items"]
    assert len(listed) == len(REST_EXAMPLES)


class _Flaky(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FlakyHandler)
        self.keys: list[str] = []


class _FlakyHandler(BaseHTTPRequestHandler):
    server: _Flaky

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.keys.append(self.headers.get("Idempotency-Key", ""))
        if len(self.server.keys) < 3:
            body, status = b"{}", 503 if len(self.server.keys) == 1 else 429
        else:
            body, status = b'{"event_id":"retried","status":"accepted"}', 202
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def test_rest_examples_retry_429_and_5xx_with_the_same_key() -> None:
    for name, command in REST_EXAMPLES.items():
        server = _Flaky()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            args = [*command, "retry question"]
            result = _run(args, "not-a-real-token", url)
            assert result["event_id"] == "retried", name
            assert len(server.keys) == 3 and len(set(server.keys)) == 1 and server.keys[0], name
        finally:
            server.shutdown()


def test_rest_python_example_stops_on_permanent_errors() -> None:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "rest_python.py")], capture_output=True, text=True,
        env={**os.environ, "KEBIVO_URL": APP, "KEBIVO_TOKEN": "kebivo_invalid"}, timeout=30,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["http_status"] == 401


def test_mcp_python_example_and_client_config(admin: Admin) -> None:
    project, token = _project(admin)
    question = record_content(unique("Example MCP question"))
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "mcp_client.py"), "--url", APP, "--question", question],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PROJECT_TOKEN": token},
    )
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[0] == {"tools": ["report_knowledge_gap"]}
    event_id = lines[1]["event_id"]
    assert admin.event(event_id)["system"]["submitted_via"] == "mcp"

    config = json.loads((EXAMPLES / "mcp-client-config.json").read_text())["mcpServers"]["kebivo"]
    assert config["type"] == "http" and config["url"].endswith("/mcp")
    assert config["headers"] == {"Authorization": "Bearer ${KEBIVO_TOKEN}"}
    expanded = config["headers"]["Authorization"].replace("${KEBIVO_TOKEN}", token)

    async def connect() -> list[str]:
        async with mcp_session(expanded.removeprefix("Bearer ")) as session:
            return [tool.name for tool in (await session.list_tools()).tools]

    assert run(connect()) == ["report_knowledge_gap"]


def test_webhook_receiver_example_rejects_bad_signature_and_schema() -> None:
    sys.path.insert(0, str(EXAMPLES))
    import base64
    import hashlib
    import hmac
    import time

    from webhook_receiver import schema_errors, verify_signature

    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    body = b'{"ok":true}'
    stamp = str(int(time.time()))
    signature = "v1," + base64.b64encode(hmac.new(
        b"k" * 32, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    headers = {"webhook-id": "msg_1", "webhook-timestamp": stamp, "webhook-signature": signature}
    assert verify_signature(headers, body, secret)
    assert not verify_signature({**headers, "webhook-signature": "v1,AAAA"}, body, secret)
    assert not verify_signature(headers, body + b" ", secret)
    assert not verify_signature({**headers, "webhook-timestamp": str(int(stamp) - 600)},
                                body, secret)
    schema = json.loads((ROOT / "docs" / "webhook-payload.schema.json").read_text())
    assert schema_errors({"schema_version": "1.0"}, schema)


def test_agent_instructions_cover_the_rest_contract() -> None:
    text = (EXAMPLES / "agent-instructions.md").read_text()
    for needle in ("/api/v1/feedback", "Idempotency-Key", '"event_type": "knowledge_gap"',
                   "knowledge_correction", "unverified", "report_knowledge_gap"):
        assert needle in text, needle
    assert "guarantee" in text


def _ui_snippets(kind: str) -> dict[str, str]:
    script = (
        f"const m = await import({json.dumps(str(SNIPPETS))});"
        f"console.log(JSON.stringify({{...m.restSnippets({json.dumps(APP)}, {json.dumps(kind)}),"
        f"MCP: m.mcpConfig({json.dumps(APP)}), sample: m.SAMPLE_QUESTION}}));"
    )
    output = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True,
                            text=True, check=True, timeout=30).stdout
    return dict(json.loads(output))


def test_ui_snippets_run_and_match_the_examples(admin: Admin, tmp_path: Path) -> None:
    project = admin.project()
    admin.patch(f"/projects/{project['id']}", {"settings": {
        "corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}})
    env = {**os.environ, "KEBIVO_URL": APP}
    for kind in ("knowledge_gap", "knowledge_correction"):
        snippets = _ui_snippets(kind)
        assert snippets["sample"] == SAMPLE_QUESTION
        assert json.loads(snippets["JSON"])["event_type"] == kind
        (tmp_path / "snippet.py").write_text(snippets["Python"])
        (tmp_path / "snippet.mts").write_text(snippets["TypeScript"])
        runs = {
            "cURL": ["bash", "-c", snippets["cURL"]],
            "Python": [sys.executable, str(tmp_path / "snippet.py")],
            "TypeScript": ["node", str(tmp_path / "snippet.mts")],
        }
        event_ids = set()
        for name, command in runs.items():
            # A fresh token per snippet keeps the shared sample Idempotency-Key apart.
            other = admin.project()
            if kind == "knowledge_correction":
                admin.patch(f"/projects/{other['id']}", {"settings": {
                    "corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}})
            token = admin.token(other["id"], ["feedback:gap", "feedback:correction"])
            result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                    env={**env, "KEBIVO_TOKEN": token})
            assert result.returncode == 0, (name, result.stderr)
            assert "202" in result.stdout, (kind, name, result.stdout)
            listed = admin.get(f"/admin/feedback?project_id={other['id']}")["items"]
            assert len(listed) == 1 and listed[0]["event_type"] == kind, (kind, name)
            event_ids.add(listed[0]["id"])
        assert len(event_ids) == 3
    config = json.loads(_ui_snippets("knowledge_gap")["MCP"])["mcpServers"]["kebivo"]
    example = json.loads((EXAMPLES / "mcp-client-config.json").read_text())["mcpServers"]["kebivo"]
    assert set(config) == set(example) and config["type"] == example["type"]
    assert config["url"] == APP + "/mcp"
    for path in ("rest_curl.sh", "rest_python.py", "rest_typescript.ts"):
        text = (EXAMPLES / path).read_text()
        assert SAMPLE_QUESTION in text and "/api/v1/feedback" in text, path
        assert "Idempotency-Key" in text, path
    assert httpx.get(APP + "/health/live", timeout=5).status_code == 200
