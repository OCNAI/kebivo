"""Bounded retention and housekeeping in the existing SQLite worker."""

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kebivo.admin_group_membership import (
    OrphanFeedbackError,
    _checkpoint_deleted_feedback,
    delete_feedback_event,
)
from kebivo.admin_group_transactions import begin_group_write
from kebivo.models import (
    AdminSession,
    AuditEvent,
    Delivery,
    DeliveryAttempt,
    FeedbackEvent,
    IdempotencyRecord,
    LoginAttempt,
    MaintenanceRun,
    ProcessingJob,
    now_utc,
)
from kebivo.settings import Settings

LOG = logging.getLogger("kebivo.request")
BATCH_EVENTS = 25
BATCH_ROWS = 500
BATCH_GAP_SECONDS = 0.1
RUN_INTERVAL = timedelta(hours=24)


class RetentionCancelled(Exception):
    """Shutdown asked the run to stop between batches."""


def _pause(stop: threading.Event | None) -> None:
    """Yield the write lock between batches; raise when shutdown is requested."""
    if stop is None:
        time.sleep(BATCH_GAP_SECONDS)
    elif stop.wait(BATCH_GAP_SECONDS):
        raise RetentionCancelled


def cutoffs(settings: Settings, now: datetime) -> dict[str, datetime | None]:
    return {
        "feedback": now - timedelta(days=settings.feedback_retention_days)
        if settings.feedback_retention_days
        else None,
        "delivery_attempts": now - timedelta(days=settings.delivery_log_retention_days)
        if settings.delivery_log_retention_days
        else None,
        "audit_events": now - timedelta(days=settings.audit_retention_days)
        if settings.audit_retention_days
        else None,
        "admin_sessions": now,
        "idempotency_records": now,
        "login_attempts": now - timedelta(minutes=15),
    }


def _base_counts() -> dict[str, int]:
    return {
        key: 0
        for key in (
            "feedback",
            "skipped_busy",
            "skipped_error",
            "delivery_attempts",
            "audit_events",
            "admin_sessions",
            "idempotency_records",
            "login_attempts",
        )
    }


def due(db: Session, now: datetime) -> bool:
    latest = db.scalar(
        select(MaintenanceRun)
        .where(MaintenanceRun.kind == "retention")
        .order_by(MaintenanceRun.started_at.desc())
        .limit(1)
    )
    if latest is None:
        return True
    if latest.status == "running":
        return latest.started_at.replace(tzinfo=UTC) < now - timedelta(hours=1)
    if latest.status == "completed" and latest.finished_at is not None:
        return latest.finished_at.replace(tzinfo=UTC) < now - RUN_INTERVAL
    return True


def preview(
    factory: sessionmaker[Session], settings: Settings, now: datetime | None = None
) -> dict[str, int]:
    """Read-only estimate; no run row or audit write."""
    limits = cutoffs(settings, now or now_utc())
    counts = _base_counts()
    with factory() as db:
        if (cutoff := limits["feedback"]) is not None:
            counts["feedback"] = (
                db.scalar(
                    select(func.count())
                    .select_from(FeedbackEvent)
                    .where(FeedbackEvent.created_at < cutoff)
                )
                or 0
            )
            busy = or_(
                exists().where(
                    ProcessingJob.event_id == FeedbackEvent.id,
                    ProcessingJob.status == "processing",
                ),
                exists().where(
                    Delivery.event_id == FeedbackEvent.id,
                    Delivery.status == "processing",
                ),
            )
            counts["skipped_busy"] = (
                db.scalar(
                    select(func.count())
                    .select_from(FeedbackEvent)
                    .where(FeedbackEvent.created_at < cutoff, busy)
                )
                or 0
            )
            counts["feedback"] -= counts["skipped_busy"]
        if (cutoff := limits["delivery_attempts"]) is not None:
            counts["delivery_attempts"] = (
                db.scalar(
                    select(func.count())
                    .select_from(DeliveryAttempt)
                    .join(Delivery)
                    .where(
                        Delivery.status.in_(("delivered", "failed")),
                        func.coalesce(DeliveryAttempt.completed_at, DeliveryAttempt.started_at)
                        < cutoff,
                    )
                )
                or 0
            )
        if (cutoff := limits["audit_events"]) is not None:
            counts["audit_events"] = (
                db.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.created_at < cutoff)
                )
                or 0
            )
        for name, model, column in (
            ("admin_sessions", AdminSession, AdminSession.expires_at),
            ("idempotency_records", IdempotencyRecord, IdempotencyRecord.expires_at),
            ("login_attempts", LoginAttempt, LoginAttempt.attempted_at),
        ):
            counts[name] = (
                db.scalar(select(func.count()).select_from(model).where(column < limits[name])) or 0
            )
    return counts


def _delete_rows(
    factory: sessionmaker[Session],
    model: Any,
    condition: Any,
    stop: threading.Event | None = None,
) -> int:
    total = 0
    while True:
        if stop is not None and stop.is_set():
            raise RetentionCancelled
        with factory() as db:
            begin_group_write(db)
            ids = list(
                db.scalars(select(model.id).where(condition).order_by(model.id).limit(BATCH_ROWS))
            )
            if ids:
                db.execute(delete(model).where(model.id.in_(ids)))
            db.commit()
        total += len(ids)
        if len(ids) < BATCH_ROWS:
            return total
        _pause(stop)


def run(
    factory: sessionmaker[Session],
    settings: Settings,
    now: datetime | None = None,
    stop: threading.Event | None = None,
) -> dict[str, int]:
    """Run now, committing at most 25 feedback events or 500 rows at a time.

    Setting ``stop`` ends the run between batches with RetentionCancelled; the
    run row is then marked failed and the next check starts a fresh run.
    """
    clock = now or now_utc()
    limits = cutoffs(settings, clock)
    counts = _base_counts()
    with factory() as db:
        begin_group_write(db)
        current = db.scalar(
            select(MaintenanceRun)
            .where(MaintenanceRun.status == "running")
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
        )
        if current and current.started_at.replace(tzinfo=UTC) >= clock - timedelta(hours=1):
            db.rollback()
            return counts
        started_run = MaintenanceRun(
            started_at=clock,
            status="running",
            cutoffs_json=json.dumps(
                {key: value.isoformat() if value else None for key, value in limits.items()},
                sort_keys=True,
            ),
            counts_json=json.dumps(counts),
        )
        db.add(started_run)
        db.flush()
        run_id = started_run.id
        db.commit()
    try:
        if (cutoff := limits["feedback"]) is not None:
            after: tuple[datetime, str] | None = None
            while True:
                if stop is not None and stop.is_set():
                    raise RetentionCancelled
                with factory() as db:
                    begin_group_write(db)
                    query = select(FeedbackEvent).where(FeedbackEvent.created_at < cutoff)
                    if after is not None:
                        query = query.where(
                            or_(
                                FeedbackEvent.created_at > after[0],
                                (FeedbackEvent.created_at == after[0])
                                & (FeedbackEvent.id > after[1]),
                            )
                        )
                    batch = list(
                        db.scalars(
                            query.order_by(FeedbackEvent.created_at, FeedbackEvent.id).limit(
                                BATCH_EVENTS
                            )
                        )
                    )
                    for event in batch:
                        after = (event.created_at, event.id)
                        try:
                            result = delete_feedback_event(db, event, "system")
                        except OrphanFeedbackError:
                            # No group membership: keep the event and let the run go on.
                            counts["skipped_error"] += 1
                            continue
                        if result is None:
                            counts["skipped_busy"] += 1
                        else:
                            counts["feedback"] += 1
                    db.commit()
                if len(batch) < BATCH_EVENTS:
                    break
                _pause(stop)
            if counts["feedback"]:
                with factory() as db:
                    _checkpoint_deleted_feedback(db)
        if (cutoff := limits["delivery_attempts"]) is not None:
            condition = DeliveryAttempt.id.in_(
                select(DeliveryAttempt.id)
                .join(Delivery)
                .where(
                    Delivery.status.in_(("delivered", "failed")),
                    func.coalesce(DeliveryAttempt.completed_at, DeliveryAttempt.started_at)
                    < cutoff,
                )
            )
            counts["delivery_attempts"] = _delete_rows(factory, DeliveryAttempt, condition, stop)
        if (cutoff := limits["audit_events"]) is not None:
            counts["audit_events"] = _delete_rows(
                factory, AuditEvent, AuditEvent.created_at < cutoff, stop
            )
        for name, model, column in (
            ("admin_sessions", AdminSession, AdminSession.expires_at),
            ("idempotency_records", IdempotencyRecord, IdempotencyRecord.expires_at),
            ("login_attempts", LoginAttempt, LoginAttempt.attempted_at),
        ):
            counts[name] = _delete_rows(factory, model, column < limits[name], stop)
        with factory() as db:
            begin_group_write(db)
            row = db.get(MaintenanceRun, run_id)
            assert row is not None
            row.status = "completed"
            row.finished_at = now_utc()
            row.counts_json = json.dumps(counts, sort_keys=True)
            db.add(
                AuditEvent(
                    actor_type="system",
                    action="retention.completed",
                    subject_type="maintenance_run",
                    subject_id=run_id,
                    details_json=json.dumps(
                        {"cutoffs": json.loads(row.cutoffs_json), "counts": counts}, sort_keys=True
                    ),
                )
            )
            older = list(
                db.scalars(
                    select(MaintenanceRun.id)
                    .where(MaintenanceRun.kind == "retention")
                    .order_by(MaintenanceRun.started_at.desc())
                    .offset(30)
                )
            )
            if older:
                db.execute(delete(MaintenanceRun).where(MaintenanceRun.id.in_(older)))
            db.commit()
        LOG.info("retention_completed", extra={"fields": counts})
        return counts
    except Exception as exc:
        with factory() as db:
            begin_group_write(db)
            row = db.get(MaintenanceRun, run_id)
            assert row is not None
            row.status = "failed"
            row.finished_at = now_utc()
            row.counts_json = json.dumps(counts, sort_keys=True)
            row.error_category = type(exc).__name__[:80]
            db.commit()
        LOG.error("retention_failed", extra={"fields": {"category": type(exc).__name__}})
        raise
