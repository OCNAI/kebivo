"""Relay Kebivo webhooks to Jira: one Jira issue per delivered report.

Usage:
    WEBHOOK_SECRET=whsec_... JIRA_BASE_URL=https://your-site.atlassian.net \
    JIRA_EMAIL=you@example.org JIRA_API_TOKEN=... JIRA_PROJECT_KEY=DOCS \
        python examples/recipes/jira/relay.py --port 9100

Optional: JIRA_ISSUE_TYPE (default Task), JIRA_LABELS (comma-separated, default
kebivo) and KEBIVO_URL for a link back to the inbox group. Uses the REST API v2
create-issue endpoint, which takes a plain-text description on Jira Cloud and
Jira Data Center. For Data Center with a personal access token, set
JIRA_BEARER_TOKEN instead of JIRA_EMAIL and JIRA_API_TOKEN.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay_common import Outbound, inbox_link, serve, title  # noqa: E402


def description(payload: dict[str, Any]) -> str:
    feedback = payload["feedback"]
    parts = [f"*Question*\n{feedback['question']}"]
    if feedback["context"]:
        parts.append(f"*Agent context*\n{feedback['context']}")
    if feedback["correction"]:
        parts.append(f"*Proposed correction (unverified)*\n{feedback['correction']}")
    facts = [
        f"Project: {payload['project']['name']}",
        f"Times asked: {payload['occurrence_count']}",
        f"Tags: {', '.join(payload['tags']) or 'none'}",
        f"Kebivo event: {payload['event_id']}",
    ]
    link = inbox_link(payload)
    if link:
        facts.append(f"[Open in Kebivo|{link}]")
    parts.append("\n".join(f"* {fact}" for fact in facts))
    return "\n\n".join(parts)


def authorization() -> str:
    if os.environ.get("JIRA_BEARER_TOKEN"):
        return f"Bearer {os.environ['JIRA_BEARER_TOKEN']}"
    pair = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()
    return "Basic " + base64.b64encode(pair).decode()


def to_jira(payload: dict[str, Any]) -> Outbound:
    # Jira labels cannot contain spaces.
    labels = [
        re.sub(r"\s+", "-", label.strip())
        for label in os.environ.get("JIRA_LABELS", "kebivo").split(",")
        if label.strip()
    ]
    return Outbound(
        url=os.environ["JIRA_BASE_URL"].rstrip("/") + "/rest/api/2/issue",
        headers={"Authorization": authorization(), "Accept": "application/json"},
        body={
            "fields": {
                "project": {"key": os.environ["JIRA_PROJECT_KEY"]},
                "issuetype": {"name": os.environ.get("JIRA_ISSUE_TYPE", "Task")},
                "summary": title(payload, 250),
                "description": description(payload),
                "labels": labels,
            }
        },
    )


if __name__ == "__main__":
    required = ["JIRA_BASE_URL", "JIRA_PROJECT_KEY"]
    if not os.environ.get("JIRA_BEARER_TOKEN"):
        required += ["JIRA_EMAIL", "JIRA_API_TOKEN"]
    serve(__doc__.splitlines()[0], to_jira, required)
