"""Administrator delivery log, manual retry, and destination test send."""

import base64
import binascii
import json
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.destinations.payload import test_payload
from kebivo.errors import error_response
from kebivo.models import (
    AuditEvent,
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackGroupMember,
    now_utc,
)
from kebivo.worker.runner import Worker, send_destination


def _id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _cursor(value: str) -> tuple[datetime, str] | None:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        timestamp, identifier = raw.split("|", 1)
        return datetime.fromisoformat(timestamp), str(UUID(identifier))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def _serial(row: Delivery) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in {
            "id": row.id,
            "event_id": row.event_id,
            "project_id": row.project_id,
            "destination_id": row.destination_id,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "max_attempts": row.max_attempts,
            "manual_retry_count": row.manual_retry_count,
            "next_attempt_at": row.next_attempt_at,
            "last_attempt_at": row.last_attempt_at,
            "last_error_category": row.last_error_category,
            "last_error_message": row.last_error_message,
            "last_http_status": row.last_http_status,
            "delivered_at": row.delivered_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }.items()
    }


def _attempt(row: DeliveryAttempt) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in {
            "id": row.id,
            "attempt_number": row.attempt_number,
            "trigger": row.trigger,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "result": row.result,
            "http_status": row.http_status,
            "error_category": row.error_category,
            "sanitized_error": row.sanitized_error,
            "duration_ms": row.duration_ms,
        }.items()
    }


def install_delivery_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/deliveries", tags=["deliveries"])

    @router.get("")
    def list_deliveries(
        request: Request,
        auth: AdminAuth,
        status: str | None = None,
        destination_id: str | None = None,
        project_id: str | None = None,
        event_id: str | None = None,
        group_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
    ) -> Any:
        del auth
        statement = select(Delivery)
        if group_id is not None:
            if request.headers.get("authorization", "").lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="Authentication required")
            canonical_group = _id(group_id)
            if canonical_group is None:
                return error_response(
                    422, "validation_error", "Invalid ID", request.state.request_id
                )
            statement = statement.where(
                Delivery.event_id.in_(
                    select(FeedbackGroupMember.event_id).where(
                        FeedbackGroupMember.group_id == canonical_group
                    )
                )
            )
        if status is not None:
            if status not in {"pending", "processing", "retry_scheduled", "delivered", "failed"}:
                return error_response(
                    422, "validation_error", "Invalid status", request.state.request_id
                )
            statement = statement.where(Delivery.status == status)
        for name, value in (
            ("destination_id", destination_id),
            ("project_id", project_id),
            ("event_id", event_id),
        ):
            if value is not None:
                canonical = _id(value)
                if canonical is None:
                    return error_response(
                        422, "validation_error", "Invalid ID", request.state.request_id
                    )
                statement = statement.where(getattr(Delivery, name) == canonical)
        if cursor is not None:
            point = _cursor(cursor)
            if point is None:
                return error_response(
                    400, "invalid_cursor", "Invalid cursor", request.state.request_id
                )
            statement = statement.where(
                or_(
                    Delivery.created_at < point[0],
                    and_(Delivery.created_at == point[0], Delivery.id < point[1]),
                )
            )
        rows = request.state.db.scalars(
            statement.order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(limit + 1)
        ).all()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            next_cursor = (
                base64.urlsafe_b64encode(
                    f"{page[-1].created_at.isoformat()}|{page[-1].id}".encode()
                )
                .decode()
                .rstrip("=")
            )
        return {"items": [_serial(row) for row in page], "next_cursor": next_cursor}

    @router.get("/{delivery_id}")
    def get_delivery(delivery_id: str, request: Request, auth: AdminAuth) -> Any:
        del auth
        row = request.state.db.get(Delivery, _id(delivery_id)) if _id(delivery_id) else None
        if row is None:
            return error_response(404, "not_found", "Delivery not found", request.state.request_id)
        attempts = request.state.db.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == row.id)
            .order_by(DeliveryAttempt.attempt_number)
        ).all()
        return {
            **_serial(row),
            "attempts": [_attempt(item) for item in attempts],
            "attempt_details_removed_by_retention": len(attempts) < row.attempt_count,
        }

    @router.post("/{delivery_id}/retry")
    async def retry(delivery_id: str, request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        row = db.get(Delivery, _id(delivery_id)) if _id(delivery_id) else None
        if row is None:
            return error_response(404, "not_found", "Delivery not found", request.state.request_id)
        destination = db.get(Destination, row.destination_id)
        if row.status != "failed" or destination is None or not destination.enabled:
            return error_response(
                409, "conflict", "Delivery cannot be retried", request.state.request_id
            )
        worker = request.app.state.worker or Worker(
            request.app.state.session_factory, request.app.state.settings
        )
        try:
            timeout = json.loads(destination.config_json).get("timeout_seconds", 10)
            if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
                timeout = 10
        except (ValueError, TypeError, AttributeError):
            timeout = 10
        row.status = "processing"
        row.lease_owner = worker.boot_id
        row.lease_until = now_utc() + timedelta(seconds=timeout + 30)
        row.manual_retry_count += 1
        db.add(
            AuditEvent(
                actor_type="admin",
                actor_ref=admin.id,
                action="delivery.manual_retry",
                subject_type="delivery",
                subject_id=row.id,
                details_json="{}",
            )
        )
        db.commit()
        await worker._deliver(row.id, trigger="manual")
        db.expire_all()
        return get_delivery(delivery_id, request, auth)

    app.include_router(router)


def install_test_route(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/destinations", tags=["destinations"])
    app.state.test_send_windows = {}

    @router.post("/{destination_id}/test")
    async def test_destination(destination_id: str, request: Request, auth: AdminAuth) -> Any:
        admin, session, _ = auth
        db: Session = request.state.db
        row = db.get(Destination, _id(destination_id)) if _id(destination_id) else None
        if row is None:
            return error_response(
                404, "not_found", "Destination not found", request.state.request_id
            )
        now = time.monotonic()
        windows: dict[str, list[float]] = request.app.state.test_send_windows
        recent = [value for value in windows.get(session.id, []) if now - value < 60]
        if len(recent) >= 10:
            return error_response(
                429, "rate_limited", "Test send limit exceeded", request.state.request_id
            )
        windows[session.id] = recent + [now]
        destination = {
            "id": row.id,
            "type": row.type,
            "config": row.config_json,
            "ciphertext": row.secrets_ciphertext,
            "allow_private_network": row.allow_private_network,
        }
        started = time.monotonic()
        result = await send_destination(destination, test_payload(), request.app.state.settings)
        db.add(
            AuditEvent(
                actor_type="admin",
                actor_ref=admin.id,
                action="destination.tested",
                subject_type="destination",
                subject_id=row.id,
                details_json=json.dumps({"outcome": result.outcome, "category": result.category}),
            )
        )
        db.commit()
        return {
            "outcome": result.outcome,
            "category": result.category,
            "http_status": result.http_status,
            "sanitized_error": result.sanitized_error,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    app.include_router(router)
