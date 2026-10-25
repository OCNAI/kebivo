"""Feedback group creation and integrity checks."""

import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kebivo.models import (
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    now_utc,
)


def associate(
    db: Session,
    event: FeedbackEvent,
    group: FeedbackGroup,
    method: str,
    confidence: float | None = None,
) -> None:
    """Join an existing group without changing its representative."""
    if event.project_id != group.project_id or event.event_type != group.event_type:
        raise ValueError("Event and group must share project and type")
    reopened = group.status == "resolved"
    db.add(
        FeedbackGroupMember(
            event_id=event.id, group_id=group.id, match_method=method, triggered_reopen=reopened
        )
    )
    db.flush()
    count, first, last = db.execute(
        select(func.count(), func.min(FeedbackEvent.created_at), func.max(FeedbackEvent.created_at))
        .join(FeedbackGroupMember, FeedbackGroupMember.event_id == FeedbackEvent.id)
        .where(FeedbackGroupMember.group_id == group.id)
    ).one()
    group.occurrence_count, group.first_seen_at, group.last_seen_at = count, first, last
    group.updated_at = now_utc()
    details: dict[str, str | float] = {"event_id": event.id, "method": method}
    if confidence is not None:
        details["confidence"] = confidence
    db.add(
        FeedbackGroupHistory(
            group_id=group.id,
            action="occurrence_added",
            actor_type="system",
            details_json=json.dumps(details, separators=(",", ":")),
        )
    )
    if reopened:
        group.status = "open"
        group.resolved_at = None
        group.status_changed_at = now_utc()
        db.add(
            FeedbackGroupHistory(
                group_id=group.id,
                action="status_changed",
                actor_type="system",
                from_status="resolved",
                to_status="open",
            )
        )


def create_initial_group(db: Session, event: FeedbackEvent) -> FeedbackGroup:
    """Attach a newly flushed event to its own group in the caller's transaction."""
    group = FeedbackGroup(
        project_id=event.project_id,
        event_type=event.event_type,
        representative_event_id=event.id,
        occurrence_count=1,
        first_seen_at=event.created_at,
        last_seen_at=event.created_at,
        status_changed_at=event.created_at,
    )
    db.add(group)
    db.flush()
    db.add_all(
        [
            FeedbackGroupMember(event_id=event.id, group_id=group.id, match_method="initial"),
            FeedbackGroupHistory(group_id=group.id, action="created", actor_type="system"),
        ]
    )
    return group


def check_integrity(db: Session) -> list[str]:
    """Return every broken membership or derived group invariant."""
    violations: list[str] = []
    missing = db.execute(
        text(
            "SELECT e.id FROM feedback_events e LEFT JOIN feedback_group_members m "
            "ON m.event_id=e.id WHERE m.event_id IS NULL ORDER BY e.id"
        )
    ).scalars()
    violations.extend(f"event {event_id}: no group" for event_id in missing)
    groups = db.execute(
        text(
            "SELECT g.id, g.project_id, g.event_type, g.representative_event_id, "
            "g.occurrence_count, g.first_seen_at, g.last_seen_at, "
            "COUNT(m.event_id), MIN(e.created_at), MAX(e.created_at), "
            "SUM(CASE WHEN e.project_id <> g.project_id "
            "OR e.event_type <> g.event_type THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN m.event_id = g.representative_event_id THEN 1 ELSE 0 END) "
            "FROM feedback_groups g LEFT JOIN feedback_group_members m ON m.group_id=g.id "
            "LEFT JOIN feedback_events e ON e.id=m.event_id GROUP BY g.id ORDER BY g.id"
        )
    ).all()
    for (
        group_id,
        _,
        _,
        _,
        count,
        first,
        last,
        actual,
        earliest,
        latest,
        mismatch,
        representative,
    ) in groups:
        if actual == 0:
            violations.append(f"group {group_id}: no members")
        if count != actual:
            violations.append(f"group {group_id}: occurrence count {count} != {actual}")
        if actual and (first != earliest or last != latest):
            violations.append(f"group {group_id}: seen timestamps differ from members")
        if not representative:
            violations.append(f"group {group_id}: representative is not a member")
        if mismatch:
            violations.append(f"group {group_id}: project or type differs from member")
    return violations
