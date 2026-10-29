"""Relay signed Kebivo webhooks to GitHub Issues."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay_common import Outbound, inbox_link, serve, title  # noqa: E402


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


def to_github(payload: dict[str, Any]) -> Outbound:
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be owner/repo")
    owner, repo = repository.split("/")
    return Outbound(
        url=f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')}/repos/"
        f"{quote(owner)}/{quote(repo)}/issues",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        },
        body={
            "title": title(payload),
            "body": description(payload),
            "labels": payload["tags"],
        },
        rate_limit_403=True,
    )


if __name__ == "__main__":
    serve(__doc__, to_github, ["GITHUB_TOKEN", "GITHUB_REPOSITORY"])
