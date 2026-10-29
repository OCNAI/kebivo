"""Relay signed Kebivo webhooks to a Microsoft Teams Workflows webhook."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay_common import Outbound, inbox_link, serve, title  # noqa: E402


def to_teams(payload: dict[str, Any]) -> Outbound:
    feedback = payload["feedback"]
    lines = [f"Project: {payload['project']['name']}"]
    if payload["occurrence_count"] > 1:
        lines.append(f"Asked {payload['occurrence_count']} times")
    if payload["tags"]:
        lines.append("Tags: " + ", ".join(payload["tags"]))
    if feedback["context"]:
        lines.append(f"Context: {feedback['context'][:1500]}")
    if feedback["correction"]:
        lines.append(f"Proposed correction (unverified): {feedback['correction'][:1500]}")
    link = inbox_link(payload)
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {"type": "TextBlock", "text": title(payload, 150), "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": "\n".join(lines), "wrap": True},
        ],
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Kebivo", "url": link}]
    return Outbound(
        url=os.environ["TEAMS_WEBHOOK_URL"],
        body={
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        },
    )


if __name__ == "__main__":
    serve(__doc__, to_teams, ["TEAMS_WEBHOOK_URL"])
