"""Run the published client example against the real HTTP test server."""

import json
import os
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
