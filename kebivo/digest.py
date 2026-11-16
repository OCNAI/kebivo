"""Weekly digest: schedule slots, due digest rows, and the payload built at send time.

The worker checks once a minute. Each digest destination stores the latest slot it has
handled; a slot is handled in one write transaction that also inserts its delivery rows,
so a missed slot is sent once on the next start and never twice.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, select, text
from sqlalchemy.orm import Session

from kebivo.models import Delivery, Destination, Project, now_utc

PERIOD = timedelta(days=7)
TOP_GROUPS = 5
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def latest_slot(now: datetime, weekday: int, hour: int) -> datetime:
    """Most recent weekday/hour in server local time at or before now, as naive UTC."""
    local = now.astimezone().replace(tzinfo=None)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(
        days=(local.weekday() - weekday) % 7
    )
    if slot > local:
        slot -= PERIOD
    # A naive datetime's astimezone() resolves it as local wall time, DST included.
    return _naive(slot.astimezone())


def period_start(slot: datetime) -> datetime:
    local = slot.replace(tzinfo=UTC).astimezone().replace(tzinfo=None) - PERIOD
    return _naive(local.astimezone())


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=UTC).isoformat()


def _summary(
    db: Session, destination_id: str, project_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    params = {
        "destination": destination_id,
        "project": project_id,
        "start": start.isoformat(sep=" "),
        "end": end.isoformat(sep=" "),
    }
    routed = """SELECT gm.group_id, e.id AS event_id, e.created_at FROM routing_matches m
        JOIN feedback_events e ON e.id=m.event_id
        JOIN feedback_group_members gm ON gm.event_id=e.id
        WHERE m.destination_id=:destination AND m.outcome='digest_queued'
          AND e.project_id=:project"""
    groups = db.execute(
        text(f"""SELECT g.id, rep.question, COUNT(DISTINCT r.event_id) AS occurrences,
                g.last_seen_at
            FROM ({routed}) r JOIN feedback_groups g ON g.id=r.group_id
            JOIN feedback_events rep ON rep.id=g.representative_event_id
            WHERE r.created_at >= :start AND r.created_at < :end
              AND g.status IN ('open','in_progress')
            GROUP BY g.id ORDER BY occurrences DESC, g.last_seen_at DESC, g.id"""),
        params,
    ).all()
    changes = db.execute(
        text(f"""SELECT
                COUNT(DISTINCT CASE WHEN h.to_status='resolved' THEN h.group_id END),
                COUNT(DISTINCT CASE WHEN h.from_status='resolved'
                    AND h.to_status IN ('open','in_progress') THEN h.group_id END)
            FROM feedback_group_history h
            WHERE h.action='status_changed' AND h.created_at >= :start AND h.created_at < :end
              AND h.group_id IN (SELECT group_id FROM ({routed}))"""),
        params,
    ).one()
    return {"groups": groups, "resolved": changes[0] or 0, "reopened": changes[1] or 0}


def _empty(summary: dict[str, Any]) -> bool:
    return not summary["groups"] and not summary["resolved"] and not summary["reopened"]


def queue_due(db: Session, now: datetime | None = None) -> int:
    """Insert digest deliveries for every slot that has passed; caller holds the write lock."""
    moment = now or now_utc()
    created = 0
    for destination in db.scalars(
        select(Destination).where(
            Destination.delivery_mode == "weekly_digest",
            Destination.enabled.is_(True),
            Destination.is_sample.is_(False),
        )
    ):
        slot = latest_slot(moment, destination.digest_weekday or 0, destination.digest_hour or 0)
        checked = destination.digest_checked_through
        if checked is not None and _naive(checked) >= slot:
            continue
        destination.digest_checked_through = slot
        if checked is None:
            # Switching to digest mode starts the schedule; the first digest is the next slot.
            continue
        start = period_start(slot)
        projects = db.execute(
            text("""SELECT DISTINCT p.id FROM routing_matches m
                JOIN feedback_events e ON e.id=m.event_id
                JOIN projects p ON p.id=e.project_id
                WHERE m.destination_id=:destination AND m.outcome='digest_queued'
                  AND p.is_sample=0 ORDER BY p.id"""),
            {"destination": destination.id},
        ).scalars()
        for project_id in projects:
            if _empty(_summary(db, destination.id, project_id, start, slot)):
                continue
            stamp = now_utc().replace(tzinfo=None).isoformat(sep=" ")
            result = db.execute(
                text("""INSERT INTO deliveries
                    (id,event_id,project_id,destination_id,status,attempt_count,max_attempts,
                     manual_retry_count,digest_period_start,digest_period_end,
                     created_at,updated_at)
                    VALUES (:id,NULL,:project,:destination,'pending',0,6,0,:start,:end,
                            :now,:now)
                    ON CONFLICT(destination_id,project_id,digest_period_end) DO NOTHING"""),
                {
                    "id": str(uuid4()),
                    "project": project_id,
                    "destination": destination.id,
                    "start": start.isoformat(sep=" "),
                    "end": slot.isoformat(sep=" "),
                    "now": stamp,
                },
            )
            created += cast(CursorResult[Any], result).rowcount
    return created


def payload(
    db: Session, delivery: Delivery, public_url: str | None, *, test: bool = False
) -> dict[str, Any]:
    assert delivery.digest_period_start is not None and delivery.digest_period_end is not None
    project = db.get(Project, delivery.project_id)
    assert project is not None
    start, end = _naive(delivery.digest_period_start), _naive(delivery.digest_period_end)
    summary = _summary(db, delivery.destination_id, project.id, start, end)
    return digest_message(
        delivery_id=delivery.id,
        project_id=project.id,
        project_name=project.name,
        start=start,
        end=end,
        groups=[
            {
                "group_id": row[0],
                "question": row[1],
                "occurrences": row[2],
                "last_seen_at": _iso(_naive(datetime.fromisoformat(str(row[3])))),
                "url": f"{public_url}/inbox/groups/{row[0]}" if public_url else None,
            }
            for row in summary["groups"][:TOP_GROUPS]
        ],
        open_group_count=len(summary["groups"]),
        resolved=summary["resolved"],
        reopened=summary["reopened"],
        test=test,
    )


def digest_message(
    *,
    delivery_id: str,
    project_id: str,
    project_name: str,
    start: datetime,
    end: datetime,
    groups: list[dict[str, Any]],
    open_group_count: int,
    resolved: int,
    reopened: int,
    test: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "delivery_id": delivery_id,
        "event_type": "weekly_digest",
        "project": {"id": project_id, "name": project_name},
        "period": {"start": _iso(start), "end": _iso(end)},
        "top_groups": groups,
        "open_group_count": open_group_count,
        "resolved_count": resolved,
        "reopened_count": reopened,
        "test": test,
    }


def test_digest_payload() -> dict[str, Any]:
    end = now_utc().replace(tzinfo=None, microsecond=0)
    return digest_message(
        delivery_id=str(uuid4()),
        project_id=str(uuid4()),
        project_name="Test project",
        start=end - PERIOD,
        end=end,
        groups=[
            {
                "group_id": str(uuid4()),
                "question": "Test weekly digest delivery",
                "occurrences": 3,
                "last_seen_at": _iso(end),
                "url": None,
            }
        ],
        open_group_count=1,
        resolved=0,
        reopened=0,
        test=True,
    )
