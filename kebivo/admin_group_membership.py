"""Atomic administrator merge, split and feedback deletion."""

import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from kebivo.admin_group_transactions import begin_group_write
from kebivo.admin_groups import _id, _reject_bearer
from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.models import (
    AuditEvent,
    Delivery,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    ProcessingJob,
    now_utc,
)

logger = logging.getLogger(__name__)


def _checkpoint_deleted_feedback(db: Session) -> None:
    """Shrink the WAL after commit, using a connection independent of the request."""
    bind = db.get_bind()
    database = (bind.engine if isinstance(bind, Connection) else bind).url.database
    if not database or database == ":memory:":
        return
    try:
        with sqlite3.connect(database, timeout=5) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        logger.info(
            "Feedback deletion WAL checkpoint result code: %s", result[0] if result else None
        )
    except sqlite3.Error:
        logger.warning("Feedback deletion WAL checkpoint failed")


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_group_ids: list[str] = Field(min_length=1, max_length=20)


class SplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[str] = Field(min_length=1, max_length=500)
    representative_event_id: str | None = None


def _error(request: Request, status: int, code: str, message: str) -> Any:
    return error_response(status, code, message, request.state.request_id)


def _recompute(db: Session, group: FeedbackGroup) -> None:
    count, first, last = db.execute(
        select(func.count(), func.min(FeedbackEvent.created_at), func.max(FeedbackEvent.created_at))
        .join(FeedbackGroupMember, FeedbackGroupMember.event_id == FeedbackEvent.id)
        .where(FeedbackGroupMember.group_id == group.id)
    ).one()
    assert count and first is not None and last is not None
    group.occurrence_count = count
    group.first_seen_at = first
    group.last_seen_at = last
    group.updated_at = now_utc()


def _history(
    db: Session, group_id: str, action: str, admin_id: str, details: dict[str, Any]
) -> None:
    db.add(
        FeedbackGroupHistory(
            group_id=group_id,
            action=action,
            actor_type="admin",
            actor_admin_id=admin_id,
            details_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
        )
    )


def _audit(
    db: Session, action: str, admin_id: str, subject_id: str, details: dict[str, Any]
) -> None:
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin_id,
            action=action,
            subject_type="feedback_group" if action != "feedback.deleted" else "feedback_event",
            subject_id=subject_id,
            details_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
        )
    )


class OrphanFeedbackError(LookupError):
    """The event has no group membership, so it cannot be deleted through the group path."""


def delete_feedback_event(db: Session, event: FeedbackEvent, actor: str) -> dict[str, Any] | None:
    """Delete one event inside an existing immediate write transaction; None means busy.

    Raises OrphanFeedbackError, before any write, when the event has no group.
    """
    member = db.get(FeedbackGroupMember, event.id)
    group = db.get(FeedbackGroup, member.group_id) if member is not None else None
    if member is None or group is None:
        raise OrphanFeedbackError(event.id)
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event.id))
    busy_delivery = db.scalar(
        select(Delivery.id).where(Delivery.event_id == event.id, Delivery.status == "processing")
    )
    if (job is not None and job.status == "processing") or busy_delivery is not None:
        return None
    delivery_statuses: dict[str, int] = {
        status: count
        for status, count in db.execute(
            select(Delivery.status, func.count())
            .where(Delivery.event_id == event.id)
            .group_by(Delivery.status)
        )
    }
    remaining = db.scalars(
        select(FeedbackGroupMember)
        .where(
            FeedbackGroupMember.group_id == group.id,
            FeedbackGroupMember.event_id != event.id,
        )
        .join(FeedbackEvent, FeedbackEvent.id == FeedbackGroupMember.event_id)
        .order_by(FeedbackEvent.created_at, FeedbackEvent.id)
    ).all()
    group_id = group.id
    group_deleted = not remaining
    if group_deleted:
        db.delete(member)
        db.flush()
        db.delete(group)
        db.flush()
    elif group.representative_event_id == event.id:
        group.representative_event_id = remaining[0].event_id
        db.add(
            FeedbackGroupHistory(
                group_id=group_id,
                action="representative_changed",
                actor_type="system",
                details_json=json.dumps(
                    {"from_event_id": event.id, "to_event_id": remaining[0].event_id}
                ),
            )
        )
        db.flush()
    # Database cascades remove tags, matches, deliveries, attempts, job,
    # idempotency and membership; the FTS delete trigger removes search text.
    db.execute(text("DELETE FROM feedback_events WHERE id=:id"), {"id": event.id})
    if not group_deleted:
        _recompute(db, group)
        if actor == "system":
            db.add(
                FeedbackGroupHistory(
                    group_id=group_id,
                    action="event_deleted",
                    actor_type="system",
                    details_json="{}",
                )
            )
        else:
            _history(db, group_id, "event_deleted", actor, {"event_id": event.id})
    if actor != "system":
        _audit(
            db,
            "feedback.deleted",
            actor,
            event.id,
            {
                "project_id": event.project_id,
                "group_id": group_id,
                "event_type": event.event_type,
                "group_deleted": group_deleted,
                "deliveries_removed_by_status": delivery_statuses,
            },
        )
    return {"group_id": group_id, "group_deleted": group_deleted}


def install_admin_group_membership_routes(app: Any) -> None:
    groups = APIRouter(prefix="/api/v1/admin/groups", dependencies=[Depends(_reject_bearer)])
    feedback = APIRouter(prefix="/api/v1/admin/feedback", dependencies=[Depends(_reject_bearer)])

    @groups.post("/{group_id}/merge")
    def merge(group_id: str, payload: MergeRequest, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        try:
            begin_group_write(db)
            target = db.get(FeedbackGroup, _id(group_id)) if _id(group_id) else None
            if target is None:
                return _error(request, 404, "not_found", "Group not found")
            source_ids = [_id(value) for value in payload.source_group_ids]
            if (
                None in source_ids
                or len(set(source_ids)) != len(source_ids)
                or target.id in source_ids
            ):
                return _error(request, 422, "merge_incompatible", "Invalid source groups")
            sources = [db.get(FeedbackGroup, source_id) for source_id in source_ids]
            if any(
                source is None
                or source.project_id != target.project_id
                or source.event_type != target.event_type
                for source in sources
            ):
                return _error(
                    request, 422, "merge_incompatible", "Groups must share project and type"
                )
            note_count = db.scalar(
                select(func.count())
                .select_from(FeedbackGroupNote)
                .where(FeedbackGroupNote.group_id.in_([target.id, *source_ids]))
            )
            if note_count is not None and note_count > 500:
                return _error(request, 422, "note_limit", "Merged group would exceed 500 notes")
            admin_id = auth[0].id
            moved_count = 0
            for source in sources:
                assert source is not None
                members = db.scalars(
                    select(FeedbackGroupMember).where(FeedbackGroupMember.group_id == source.id)
                ).all()
                for member in members:
                    member.group_id = target.id
                    member.match_method = "manual_merge"
                moved_count += len(members)
                for note in db.scalars(
                    select(FeedbackGroupNote).where(FeedbackGroupNote.group_id == source.id)
                ):
                    note.group_id = target.id
                for row in db.scalars(
                    select(FeedbackGroupHistory).where(FeedbackGroupHistory.group_id == source.id)
                ):
                    row.group_id = target.id
                    row.origin_group_id = source.id
                db.flush()
                db.delete(source)
                db.flush()
            _recompute(db, target)
            details = {
                "target_group_id": target.id,
                "source_group_ids": source_ids,
                "source_statuses": {source.id: source.status for source in sources if source},
                "moved_events": moved_count,
            }
            _history(db, target.id, "merged", admin_id, details)
            _audit(db, "feedback_group.merged", admin_id, target.id, details)
            db.commit()
            return {"group_id": target.id, "occurrence_count": target.occurrence_count}
        except BaseException:
            db.rollback()
            raise
        finally:
            if db.in_transaction():
                db.rollback()

    @groups.post("/{group_id}/split", status_code=201)
    def split(group_id: str, payload: SplitRequest, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        try:
            begin_group_write(db)
            source = db.get(FeedbackGroup, _id(group_id)) if _id(group_id) else None
            if source is None:
                return _error(request, 404, "not_found", "Group not found")
            event_ids = [_id(value) for value in payload.event_ids]
            if None in event_ids or len(set(event_ids)) != len(event_ids):
                return _error(request, 422, "validation_error", "Invalid event IDs")
            selected_ids = [event_id for event_id in event_ids if event_id is not None]
            members = db.scalars(
                select(FeedbackGroupMember).where(FeedbackGroupMember.group_id == source.id)
            ).all()
            by_id = {member.event_id: member for member in members}
            if not set(selected_ids).issubset(by_id):
                return _error(request, 422, "validation_error", "Events must belong to group")
            if len(selected_ids) == len(members):
                return _error(request, 422, "split_all_members", "At least one member must stay")
            representative = payload.representative_event_id or selected_ids[0]
            if representative not in selected_ids:
                return _error(request, 422, "validation_error", "Representative must be split out")
            first_event = db.get(FeedbackEvent, representative)
            assert first_event is not None
            new_group = FeedbackGroup(
                project_id=source.project_id,
                event_type=source.event_type,
                representative_event_id=representative,
                occurrence_count=1,
                first_seen_at=first_event.created_at,
                last_seen_at=first_event.created_at,
                status_changed_at=now_utc(),
            )
            db.add(new_group)
            db.flush()
            if source.representative_event_id in selected_ids:
                source.representative_event_id = next(
                    member.event_id for member in members if member.event_id not in selected_ids
                )
            for event_id in selected_ids:
                by_id[event_id].group_id = new_group.id
                by_id[event_id].match_method = "manual_split"
            db.flush()
            _recompute(db, source)
            _recompute(db, new_group)
            details = {
                "source_group_id": source.id,
                "new_group_id": new_group.id,
                "moved_events": len(selected_ids),
                "representative_event_id": representative,
            }
            _history(db, source.id, "split_out", auth[0].id, details)
            _history(db, new_group.id, "split_in", auth[0].id, details)
            _audit(db, "feedback_group.split", auth[0].id, source.id, details)
            _audit(db, "feedback_group.split", auth[0].id, new_group.id, details)
            db.commit()
            return {
                "group_id": new_group.id,
                "source_group_id": source.id,
                "occurrence_count": new_group.occurrence_count,
            }
        except BaseException:
            db.rollback()
            raise
        finally:
            if db.in_transaction():
                db.rollback()

    @feedback.delete("/{event_id}")
    def delete_feedback(event_id: str, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        try:
            begin_group_write(db)
            event = db.get(FeedbackEvent, _id(event_id)) if _id(event_id) else None
            if event is None:
                return _error(request, 404, "not_found", "Feedback not found")
            result = delete_feedback_event(db, event, auth[0].id)
            if result is None:
                return _error(request, 409, "feedback_busy", "Feedback is being processed")
            db.commit()
            _checkpoint_deleted_feedback(db)
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            if db.in_transaction():
                db.rollback()

    app.include_router(groups)
    app.include_router(feedback)
