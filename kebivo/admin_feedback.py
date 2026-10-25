"""Read-only administrator view of stored feedback."""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.models import (
    ApiToken,
    Delivery,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupMember,
    ProcessingJob,
    Project,
    RoutingMatch,
    Tag,
)


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        timestamp, event_id = raw.split("|", 1)
        return datetime.fromisoformat(timestamp), str(UUID(event_id))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


def _encode_cursor(event: FeedbackEvent) -> str:
    raw = f"{event.created_at.isoformat()}|{event.id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _iso_utc(value: datetime) -> str:
    return (
        value.replace(tzinfo=UTC).isoformat()
        if value.tzinfo is None
        else value.astimezone(UTC).isoformat()
    )


def _project(project: Project) -> dict[str, str]:
    return {"id": project.id, "name": project.name, "slug": project.slug}


def _tags(db: Session, event_id: str, *, provenance: bool) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Tag, FeedbackEventTag)
        .join(FeedbackEventTag, FeedbackEventTag.tag_id == Tag.id)
        .where(FeedbackEventTag.event_id == event_id)
        .order_by(Tag.slug, Tag.id)
    ).all()
    result = []
    for tag, link in rows:
        item: dict[str, Any] = {"slug": tag.slug, "name": tag.name}
        if provenance:
            item.update(
                from_default=link.from_default,
                from_submission=link.from_submission,
                from_admin=link.from_admin,
            )
        result.append(item)
    return result


def serialize_event_detail(db: Session, event: FeedbackEvent) -> dict[str, Any]:
    project = db.get(Project, event.project_id)
    assert project is not None
    token = db.get(ApiToken, event.api_token_id) if event.api_token_id else None
    group = db.scalar(
        select(FeedbackGroup)
        .join(FeedbackGroupMember, FeedbackGroupMember.group_id == FeedbackGroup.id)
        .where(FeedbackGroupMember.event_id == event.id)
    )
    return {
        "id": event.id,
        "group": {"id": group.id, "status": group.status} if group else None,
        "event_type": event.event_type,
        "tags": _tags(db, event.id, provenance=True),
        "agent_reported": {
            "question": event.question,
            "correction": event.correction,
            "previous_answer": event.previous_answer,
            "context": event.context,
            "source_reference": event.source_reference,
            "agent_id": event.agent_id,
            "conversation_id": event.conversation_id,
            "source_event_id": event.source_event_id,
            "user_reference": event.user_reference,
            "metadata": json.loads(event.metadata_json),
        },
        "system": {
            "project": _project(project),
            "submitted_via": event.submitted_via,
            "api_token": (
                {"id": token.id, "name": token.name, "prefix": token.token_prefix}
                if token is not None
                else None
            ),
            "idempotency_key": event.idempotency_key,
            "received_at": _iso_utc(event.created_at),
            "processing_status": event.processing_status,
        },
        "routing": {
            "processing_status": event.processing_status,
            "processing_error_code": (
                job.error_code
                if (
                    job := db.scalar(
                        select(ProcessingJob).where(ProcessingJob.event_id == event.id)
                    )
                )
                else None
            ),
            "matches": [
                {
                    "id": match.id,
                    "rule_id": match.rule_id,
                    "rule_name": match.rule_name,
                    "rule_order": match.rule_order,
                    "via": match.via,
                    "destination_id": match.destination_id,
                    "outcome": match.outcome,
                }
                for match in db.scalars(
                    select(RoutingMatch)
                    .where(RoutingMatch.event_id == event.id)
                    .order_by(RoutingMatch.rule_order, RoutingMatch.id)
                )
            ],
            "deliveries": [
                {
                    "id": delivery.id,
                    "destination_id": delivery.destination_id,
                    "status": delivery.status,
                    "attempt_count": delivery.attempt_count,
                }
                for delivery in db.scalars(
                    select(Delivery)
                    .where(Delivery.event_id == event.id)
                    .order_by(Delivery.created_at, Delivery.id)
                )
            ],
        },
    }


def install_admin_feedback_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/admin/feedback", tags=["admin-feedback"])

    def _authorize(request: Request) -> None:
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")

    @router.get("")
    def list_feedback(
        request: Request,
        response: Response,
        auth: AdminAuth,
        project_id: str | None = None,
        event_type: str | None = None,
        submitted_via: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        del auth
        _authorize(request)
        db: Session = request.state.db
        statement = (
            select(FeedbackEvent, Project, FeedbackGroupMember.group_id)
            .join(Project, FeedbackEvent.project_id == Project.id)
            .outerjoin(FeedbackGroupMember, FeedbackGroupMember.event_id == FeedbackEvent.id)
        )
        if project_id is not None:
            statement = statement.where(FeedbackEvent.project_id == _uuid(project_id))
        if event_type is not None:
            if event_type not in {"knowledge_gap", "knowledge_correction"}:
                raise HTTPException(status_code=422, detail="Invalid event type")
            statement = statement.where(FeedbackEvent.event_type == event_type)
        if submitted_via is not None:
            if submitted_via not in {"rest", "mcp"}:
                raise HTTPException(status_code=422, detail="Invalid submission channel")
            statement = statement.where(FeedbackEvent.submitted_via == submitted_via)
        if cursor is not None:
            created_at, event_id = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    FeedbackEvent.created_at < created_at,
                    and_(FeedbackEvent.created_at == created_at, FeedbackEvent.id < event_id),
                )
            )
        rows = db.execute(
            statement.order_by(FeedbackEvent.created_at.desc(), FeedbackEvent.id.desc()).limit(
                limit + 1
            )
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        event_ids = [event.id for event, _, _ in rows]
        tags_by_event: dict[str, list[dict[str, str]]] = {event_id: [] for event_id in event_ids}
        if event_ids:
            tag_rows = db.execute(
                select(FeedbackEventTag.event_id, Tag.slug, Tag.name)
                .join(Tag, FeedbackEventTag.tag_id == Tag.id)
                .where(FeedbackEventTag.event_id.in_(event_ids))
                .order_by(Tag.slug, Tag.id)
            ).all()
            for event_id, slug, name in tag_rows:
                tags_by_event[event_id].append({"slug": slug, "name": name})
        response.headers["Cache-Control"] = "no-store"
        return {
            "items": [
                {
                    "id": event.id,
                    "group_id": group_id,
                    "project": _project(project),
                    "event_type": event.event_type,
                    "submitted_via": event.submitted_via,
                    "question_excerpt": event.question[:280],
                    "question_length": len(event.question),
                    "tags": tags_by_event[event.id],
                    "agent_id": event.agent_id,
                    "processing_status": event.processing_status,
                    "created_at": _iso_utc(event.created_at),
                }
                for event, project, group_id in rows
            ],
            "next_cursor": _encode_cursor(rows[-1][0]) if has_more else None,
        }

    @router.get("/{event_id}")
    def get_feedback(
        event_id: str, request: Request, response: Response, auth: AdminAuth
    ) -> dict[str, Any]:
        del auth
        _authorize(request)
        try:
            canonical_id = str(UUID(event_id))
        except ValueError:
            raise HTTPException(status_code=404, detail="Feedback not found") from None
        db: Session = request.state.db
        event = db.get(FeedbackEvent, canonical_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Feedback not found")
        response.headers["Cache-Control"] = "no-store"
        return serialize_event_detail(db, event)

    app.include_router(router)
