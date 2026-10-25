"""Relay Kebivo webhooks to Linear: one Linear issue per delivered report.

Usage:
    WEBHOOK_SECRET=whsec_... LINEAR_API_KEY=lin_api_... LINEAR_TEAM_ID=<team UUID> \
        python examples/recipes/linear/relay.py --port 9100

Optional: LINEAR_LABEL_IDS (comma-separated label UUIDs), LINEAR_API_URL
(defaults to https://api.linear.app/graphql) and KEBIVO_URL for a link back to
the inbox group.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay_common import Outbound, inbox_link, serve, title  # noqa: E402

MUTATION = """
mutation KebivoIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { identifier url } }
}
"""


def description(payload: dict[str, Any]) -> str:
    feedback = payload["feedback"]
    parts = [f"**Question**\n\n{feedback['question']}"]
    if feedback["context"]:
        parts.append(f"**Agent context**\n\n{feedback['context']}")
    if feedback["correction"]:
        parts.append(f"**Proposed correction (unverified)**\n\n{feedback['correction']}")
    facts = [
        f"Project: {payload['project']['name']}",
        f"Times asked: {payload['occurrence_count']}",
        f"Tags: {', '.join(payload['tags']) or 'none'}",
        f"Kebivo event: `{payload['event_id']}`",
    ]
    link = inbox_link(payload)
    if link:
        facts.append(f"[Open in Kebivo]({link})")
    parts.append("\n".join(f"- {fact}" for fact in facts))
    return "\n\n".join(parts)


def created(answer: bytes) -> bool:
    """Linear reports GraphQL errors with HTTP 200, so check the result."""
    try:
        result = json.loads(answer)
        return bool(result["data"]["issueCreate"]["success"])
    except (ValueError, KeyError, TypeError):
        return False


def to_linear(payload: dict[str, Any]) -> Outbound:
    issue: dict[str, Any] = {
        "teamId": os.environ["LINEAR_TEAM_ID"],
        "title": title(payload),
        "description": description(payload),
    }
    labels = [value.strip() for value in os.environ.get("LINEAR_LABEL_IDS", "").split(",")]
    if any(labels):
        issue["labelIds"] = [value for value in labels if value]
    return Outbound(
        url=os.environ.get("LINEAR_API_URL", "https://api.linear.app/graphql"),
        # Personal API keys go in the Authorization header without "Bearer".
        headers={"Authorization": os.environ["LINEAR_API_KEY"]},
        body={"query": MUTATION, "variables": {"input": issue}},
        accepted=created,
    )


if __name__ == "__main__":
    serve(__doc__.splitlines()[0], to_linear, ["LINEAR_API_KEY", "LINEAR_TEAM_ID"])
