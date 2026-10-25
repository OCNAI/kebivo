"""AC-03: events tagged `it` reach an HTTP endpoint as valid, signed JSON.

The endpoint is examples/webhook_receiver.py, which checks the signature and
the documented schema itself.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import RUNNER, Admin, gap, record_content, unique

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_ac_03_it_tag_reaches_receiver_with_valid_schema_and_signature(admin: Admin) -> None:
    project = admin.project()
    destination = admin.webhook("unused", config={"url": f"http://{RUNNER}:9103/hook"})
    admin.rule(project["id"], ["it"], [destination["id"]])
    token = admin.token(project["id"])
    receiver = subprocess.Popen(
        [sys.executable, str(EXAMPLES / "webhook_receiver.py"), "--host", "0.0.0.0",
         "--port", "9103", "--exit-after", "1"],
        env={**os.environ, "WEBHOOK_SECRET": destination["signing_secret"]},
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert receiver.stdout is not None
        assert "listening" in receiver.stdout.readline()
        question = record_content(unique("AC03 VPN client crashes on start"))
        event_id = gap(token, question, ["it"])
        admin.delivered(event_id)
        output, _ = receiver.communicate(timeout=30)
    finally:
        receiver.kill()
    accepted = json.loads(output.strip().splitlines()[-1])
    assert accepted == {
        "webhook_id": admin.deliveries(event_id)[0]["id"], "event_id": event_id,
        "event_type": "knowledge_gap", "question": question,
        "signature": "valid", "schema": "valid",
    }
