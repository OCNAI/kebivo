"""Administrator settings and scoped JSON Lines export."""

import json
from collections.abc import Iterator
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kebivo import version
from kebivo.admin_feedback import _iso_utc
from kebivo.admin_groups import _id, _reject_bearer
from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.feedback_guard import MAX_FEEDBACK_BODY
from kebivo.models import (
    AuditEvent,
    Delivery,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupMember,
    MaintenanceRun,
    Project,
    Tag,
)
from kebivo.retention import RUN_INTERVAL
from kebivo.settings import Settings


def settings_payload(db: Session, settings: Settings) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(MaintenanceRun)
            .where(MaintenanceRun.kind == "retention")
            .order_by(MaintenanceRun.started_at.desc())
            .limit(5)
        )
    )
    last_completed = next((row for row in rows if row.status == "completed"), None)
    next_run = (
        last_completed.finished_at.replace(tzinfo=UTC) + RUN_INTERVAL
        if last_completed and last_completed.finished_at
        else None
    )
    return {
        "version": version(),
        "retention": {
            "feedback_days": settings.feedback_retention_days,
            "delivery_log_days": settings.delivery_log_retention_days,
            "audit_days": settings.audit_retention_days,
            "next_run_at": next_run.isoformat() if next_run else None,
            "last_runs": [
                {
                    "started_at": _iso_utc(row.started_at),
                    "finished_at": _iso_utc(row.finished_at) if row.finished_at else None,
                    "status": row.status,
                    "cutoffs": json.loads(row.cutoffs_json),
                    "counts": json.loads(row.counts_json),
                    "error_category": row.error_category,
                }
                for row in rows
            ],
        },
        "operations": {
            "mcp_enabled": settings.mcp_enabled,
            "feedback_rate_limit_per_token": settings.feedback_rate_limit_per_token,
            "feedback_rate_limit_per_ip": settings.feedback_rate_limit_per_ip,
            "body_limit_bytes": MAX_FEEDBACK_BODY,
            "private_webhook_default": False,
            "trusted_proxies_count": len(settings.trusted_proxy_ips),
        },
    }


def _line(db: Session, event: FeedbackEvent) -> str:
    member = db.get(FeedbackGroupMember, event.id)
    group = db.get(FeedbackGroup, member.group_id) if member else None
    tags = list(
        db.scalars(
            select(Tag.slug)
            .join(FeedbackEventTag, FeedbackEventTag.tag_id == Tag.id)
            .where(FeedbackEventTag.event_id == event.id)
            .order_by(Tag.slug)
        )
    )
    deliveries = [
        {
            "status": row.status,
            "attempt_count": row.attempt_count,
            "destination_id": row.destination_id,
        }
        for row in db.scalars(
            select(Delivery)
            .where(Delivery.event_id == event.id)
            .order_by(Delivery.created_at, Delivery.id)
        )
    ]
    payload = {
        "id": event.id,
        "project_id": event.project_id,
        "event_type": event.event_type,
        "submitted_via": event.submitted_via,
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
        "processing_status": event.processing_status,
        "created_at": _iso_utc(event.created_at),
        "updated_at": _iso_utc(event.updated_at),
        "tags": tags,
        "group": {"id": group.id, "status": group.status, "match_method": member.match_method}
        if group and member
        else None,
        "deliveries": deliveries,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def install_admin_settings_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(_reject_bearer)])

    @router.get("/settings")
    def get_settings(request: Request, auth: AdminAuth) -> dict[str, Any]:
        db: Session = request.state.db
        return settings_payload(db, request.app.state.settings)

    @router.get("/projects/{project_id}/export", response_model=None)
    def export(project_id: str, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        identifier = _id(project_id)
        project = db.get(Project, identifier) if identifier else None
        if project is None:
            return error_response(404, "not_found", "Project not found", request.state.request_id)
        db.add(
            AuditEvent(
                actor_type="admin",
                actor_ref=auth[0].id,
                action="project.exported",
                subject_type="project",
                subject_id=project.id,
                details_json="{}",
            )
        )
        db.commit()
        factory = request.app.state.session_factory

        def lines() -> Iterator[str]:
            with factory() as stream_db:
                # Keyset paging keeps memory bounded and avoids a long read transaction.
                after: tuple[Any, str] | None = None
                while True:
                    query = select(FeedbackEvent).where(FeedbackEvent.project_id == identifier)
                    if after is not None:
                        from sqlalchemy import or_

                        query = query.where(
                            or_(
                                FeedbackEvent.created_at > after[0],
                                (FeedbackEvent.created_at == after[0])
                                & (FeedbackEvent.id > after[1]),
                            )
                        )
                    rows = list(
                        stream_db.scalars(
                            query.order_by(FeedbackEvent.created_at, FeedbackEvent.id).limit(100)
                        )
                    )
                    for event in rows:
                        after = (event.created_at, event.id)
                        yield _line(stream_db, event)
                    stream_db.expunge_all()
                    if len(rows) < 100:
                        break

        return StreamingResponse(
            lines(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="project-{identifier}.jsonl"',
                "Cache-Control": "no-store",
            },
        )

    app.include_router(router)
