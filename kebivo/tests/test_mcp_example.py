"""Run the published client example against the real HTTP test server."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from kebivo.models import FeedbackEvent

ROOT = Path(__file__).resolve().parents[2]
pytest_plugins = ("kebivo.tests.test_mcp",)


def test_example_client(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    origin, app, _, gap_token, _ = running
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "mcp_client.py"),
            "--url",
            origin,
            "--question",
            "Where is the VPN setup guide?",
        ],
        env={**os.environ, "PROJECT_TOKEN": gap_token},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[0] == {"tools": ["report_knowledge_gap"]}
    assert lines[1]["status"] == "accepted"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == 1
        event = db.get(FeedbackEvent, lines[1]["event_id"])
        assert event is not None and event.submitted_via == "mcp"


def test_example_client_bad_token(running: tuple[str, Any, dict[str, Any], str, str]) -> None:
    origin, _, _, _, _ = running
    token = "kebivo_pt_unknown.never-echo-this"
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "mcp_client.py"), "--url", origin],
        env={**os.environ, "PROJECT_TOKEN": token},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert "401" in result.stderr
    assert token not in result.stdout + result.stderr


def test_example_client_unreachable_port() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "mcp_client.py"), "--url", origin],
        env={**os.environ, "PROJECT_TOKEN": "unused-test-token"},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.splitlines() == [f"Could not connect to {origin}/mcp: is Kebivo running?"]
    assert "Traceback" not in result.stderr
