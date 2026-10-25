"""Versioned, explicitly allowlisted webhook payload."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def payload_v1(
    *,
    delivery_id: str,
    event_id: str,
    event_type: str,
    project_id: str,
    project_name: str,
    question: str,
    correction: str | None,
    context: str | None,
    tags: list[str],
    created_at: datetime,
    group_id: str | None = None,
    occurrence_count: int = 1,
    test: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "delivery_id": delivery_id,
        "event_id": event_id,
        "group_id": group_id,
        "event_type": event_type,
        "project": {"id": project_id, "name": project_name},
        "feedback": {"question": question, "correction": correction, "context": context},
        "tags": tags,
        "occurrence_count": occurrence_count,
        "created_at": created_at.replace(tzinfo=created_at.tzinfo or UTC).isoformat(),
        "test": test,
    }


def test_payload() -> dict[str, Any]:
    return payload_v1(
        delivery_id=str(uuid4()),
        event_id=str(uuid4()),
        event_type="knowledge_gap",
        project_id=str(uuid4()),
        project_name="Test project",
        question="Test webhook delivery",
        correction=None,
        context=None,
        tags=[],
        created_at=datetime.now(UTC),
        test=True,
    )
