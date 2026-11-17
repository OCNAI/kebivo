"""Read-only gap listing for project tokens with the gaps:read scope (ADR A9)."""

import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from kebivo.admin_feedback import _iso_utc
from kebivo.admin_groups import STATUSES, _cursor_decode, _cursor_encode, _tag_rows, _utc
from kebivo.auth import _peer_ip
from kebivo.errors import error_response
from kebivo.models import AuditEvent, FeedbackEvent, FeedbackGroup
from kebivo.tokens import (
    GAPS_READ_SCOPE,
    MISSING_TOKEN_MESSAGE,
    ProjectTokenPrincipal,
    authenticate_project_token,
    touch_project_token,
)

SORTS = ("occurrences", "last_seen")
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
CURSOR_CHARS = 1024
QUESTION_CHARS = 1000
STATUS_QUERY = Query(default=[])


class GapsQueryError(Exception):
    def __init__(self, code: str, status: int, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.field = field


def check_gaps_read_rate_limit(app: Any, token_id: str | None) -> None:
    """Per-token read budget shared by REST and MCP; raises HTTPException 429."""
    app.state.feedback_limiter.check(
        "gaps_read", token_id, app.state.settings.gaps_read_rate_limit_per_token
    )


def _serialize(group: FeedbackGroup, event: FeedbackEvent, tags: list[str]) -> dict[str, Any]:
    # Explicit allow-list: never add notes, user_reference, context or other event fields.
    return {
        "id": group.id,
        "question": event.question[:QUESTION_CHARS],
        "occurrence_count": group.occurrence_count,
        "first_seen_at": _iso_utc(group.first_seen_at),
        "last_seen_at": _iso_utc(group.last_seen_at),
        "tags": tags[:20],
        "status": group.status,
    }


def list_gaps(
    db: Session,
    project_id: str,
    *,
    status: list[str] | None = None,
    sort: str = "occurrences",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    if any(not isinstance(item, str) or item not in STATUSES for item in status or []):
        raise GapsQueryError("validation_error", 422, "status")
    statuses = sorted(set(status or ["open"]))
    if not isinstance(sort, str) or sort not in SORTS:
        raise GapsQueryError("validation_error", 422, "sort")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise GapsQueryError("validation_error", 422, "limit")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > CURSOR_CHARS):
        raise GapsQueryError("validation_error", 422, "cursor")
    filters = {"project_id": project_id, "status": statuses, "sort": sort}
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    order: list[tuple[Any, str]] = (
        [
            (FeedbackGroup.occurrence_count, "occurrence_count"),
            (FeedbackGroup.last_seen_at, "last_seen_at"),
        ]
        if sort == "occurrences"
        else [(FeedbackGroup.last_seen_at, "last_seen_at")]
    )
    statement = (
        select(FeedbackGroup, FeedbackEvent)
        .join(FeedbackEvent, FeedbackEvent.id == FeedbackGroup.representative_event_id)
        .where(
            FeedbackGroup.project_id == project_id,
            FeedbackGroup.event_type == "knowledge_gap",
            FeedbackGroup.status.in_(statuses),
        )
    )
    if cursor is not None:
        point = _cursor_decode(cursor, digest, len(order))
        if point is None:
            raise GapsQueryError("invalid_cursor", 400)
        values, identifier = point
        try:
            parsed = [
                _utc(datetime.fromisoformat(value)) if name.endswith("_at") else value
                for (_, name), value in zip(order, values, strict=True)
            ]
        except (TypeError, ValueError, OverflowError):
            raise GapsQueryError("invalid_cursor", 400) from None
        if any(type(value) not in {datetime, int} for value in parsed):
            raise GapsQueryError("invalid_cursor", 400)
        prefix: list[ColumnElement[bool]] = []
        branches: list[ColumnElement[bool]] = []
        for (column, _), value in zip(order, parsed, strict=True):
            branches.append(and_(*prefix, column < value))
            prefix.append(column == value)
        branches.append(and_(*prefix, FeedbackGroup.id < identifier))
        statement = statement.where(or_(*branches))
    statement = statement.order_by(
        *[column.desc() for column, _ in order], FeedbackGroup.id.desc()
    ).limit(limit + 1)
    rows = db.execute(statement).all()
    page = rows[:limit]
    tags = _tag_rows(db, [group.id for group, _ in page])
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        values = [
            getattr(last, name).isoformat() if name.endswith("_at") else getattr(last, name)
            for _, name in order
        ]
        next_cursor = _cursor_encode(values, last.id, digest)
    return {
        "items": [
            _serialize(group, event, [tag["slug"] for tag in tags[group.id]])
            for group, event in page
        ],
        "next_cursor": next_cursor,
    }


def record_gaps_read(
    db: Session,
    principal: ProjectTokenPrincipal,
    *,
    via: str,
    status: list[str] | None,
    sort: str,
    returned: int,
    paged: bool,
) -> None:
    """Stage one audit row for a successful read; no question text or group ids."""
    db.add(
        AuditEvent(
            actor_type="project_token",
            actor_ref=principal.token_id,
            action="gaps.read",
            subject_type="project",
            subject_id=principal.project_id,
            details_json=json.dumps(
                {
                    "via": via,
                    "status": sorted(set(status or ["open"])),
                    "sort": sort,
                    "returned": returned,
                    "paged": paged,
                },
                separators=(",", ":"),
            ),
        )
    )
    touch_project_token(db, principal.token_id)


def install_gaps_read_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/groups", tags=["gaps"])

    @router.get("")
    def read_gaps(
        request: Request,
        status: list[str] = STATUS_QUERY,
        sort: str = "occurrences",
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        cursor: str | None = Query(None, max_length=CURSOR_CHARS),
    ) -> Any:
        request.app.state.feedback_limiter.check(
            "ip", _peer_ip(request), request.app.state.settings.feedback_rate_limit_per_ip
        )
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token:
            raise HTTPException(status_code=401, detail=MISSING_TOKEN_MESSAGE)
        principal, _project = authenticate_project_token(request, token)
        if GAPS_READ_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail="Project token scope required")
        check_gaps_read_rate_limit(request.app, principal.token_id)
        db: Session = request.state.db
        try:
            result = list_gaps(
                db, principal.project_id, status=status, sort=sort, limit=limit, cursor=cursor
            )
        except GapsQueryError as exc:
            fields = [{"field": exc.field, "code": "invalid", "message": "Invalid value"}]
            return error_response(
                exc.status,
                exc.code,
                "Invalid gaps query",
                request.state.request_id,
                {"fields": fields} if exc.field else None,
            )
        record_gaps_read(
            db,
            principal,
            via="rest",
            status=status,
            sort=sort,
            returned=len(result["items"]),
            paged=cursor is not None,
        )
        db.commit()
        return result

    app.include_router(router)
