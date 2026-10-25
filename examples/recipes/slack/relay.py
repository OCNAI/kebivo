"""Relay Kebivo webhooks to a Slack channel through a Slack incoming webhook.

Usage:
    WEBHOOK_SECRET=whsec_... SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... \
        python examples/recipes/slack/relay.py --port 9100

Optional: KEBIVO_URL=https://feedback.example.org adds a link to the inbox group.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay_common import Outbound, inbox_link, serve, title  # noqa: E402


def escape(text: str) -> str:
    """Slack mrkdwn treats &, < and > as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_slack(payload: dict[str, Any]) -> Outbound:
    feedback = payload["feedback"]
    lines = [f"*{escape(title(payload, 150))}*", f"Project: {escape(payload['project']['name'])}"]
    if payload["occurrence_count"] > 1:
        lines.append(f"Asked {payload['occurrence_count']} times")
    if payload["tags"]:
        lines.append("Tags: " + ", ".join(escape(tag) for tag in payload["tags"]))
    if feedback["context"]:
        lines.append(f"Context: {escape(feedback['context'][:1500])}")
    if feedback["correction"]:
        lines.append(f"Proposed correction (unverified): {escape(feedback['correction'][:1500])}")
    link = inbox_link(payload)
    if link:
        lines.append(f"<{link}|Open in Kebivo>")
    text = "\n".join(lines)
    return Outbound(
        url=os.environ["SLACK_WEBHOOK_URL"],
        body={
            "text": text,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        },
    )


if __name__ == "__main__":
    serve(__doc__.splitlines()[0], to_slack, ["SLACK_WEBHOOK_URL"])
