"""Administrator reads for feedback groups."""

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Integer, and_, case, distinct, func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from kebivo.admin_feedback import _iso_utc, _project, serialize_event_detail
from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.feedback.tags import normalize_tag_v1
from kebivo.models import (
    DedupEvaluation,
    Delivery,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    Project,
    Tag,
)

STATUS_QUERY = Query(default=[])
TAG_QUERY = Query(default=[])

STATUSES = ("open", "in_progress", "resolved", "dismissed")
SORTS = {
    "last_activity_desc": (("last_seen_at", True),),
    "last_activity_asc": (("last_seen_at", False),),
    "occurrences_desc": (("occurrence_count", True), ("last_seen_at", True)),
    "occurrences_asc": (("occurrence_count", False), ("last_seen_at", True)),
    "status": (("status_rank", False), ("last_seen_at", True)),
    "first_reported_desc": (("first_seen_at", True),),
}


def _id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    """SQLite stores UTC wall-clock values without an offset."""
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


def _bad(request: Request, code: str = "validation_error", status: int = 422) -> Any:
    return error_response(status, code, "Invalid group query", request.state.request_id)


def _fts(q: str) -> str | None:
    # Only Unicode word tokens enter MATCH. Quotes and operators in user text are data.
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    if not tokens:
        return None
    return " AND ".join(
        '"' + token.replace('"', '""') + '"' + ("*" if i == len(tokens) - 1 else "")
        for i, token in enumerate(tokens)
    )


def _cursor_encode(values: list[Any], group_id: str, digest: str) -> str:
    payload = json.dumps({"v": values, "id": group_id, "h": digest}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _cursor_decode(cursor: str, digest: str, length: int) -> tuple[list[Any], str] | None:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (
            not isinstance(data, dict)
            or data.get("h") != digest
            or not isinstance(data.get("v"), list)
            or len(data["v"]) != length
            or any(type(value) is int and not 0 <= value < 2**31 for value in data["v"])
            or not isinstance(data.get("id"), str)
            or _id(data["id"]) != data["id"]
        ):
            return None
        return data["v"], data["id"]
    except (ValueError, OverflowError, UnicodeDecodeError, binascii.Error, TypeError):
        return None


def _group(db: Session, value: str) -> FeedbackGroup | None:
    return db.get(FeedbackGroup, _id(value)) if _id(value) else None


def _reject_bearer(request: Request) -> None:
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")


def _tag_rows(db: Session, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in ids}
    if not ids:
        return result
    rows = db.execute(
        select(
            FeedbackGroupMember.group_id,
            Tag.slug,
            Tag.name,
            func.sum(FeedbackEventTag.from_default.cast(Integer)),
            func.sum(FeedbackEventTag.from_submission.cast(Integer)),
            func.sum(FeedbackEventTag.from_admin.cast(Integer)),
        )
        .join(FeedbackEventTag, FeedbackEventTag.event_id == FeedbackGroupMember.event_id)
        .join(Tag, Tag.id == FeedbackEventTag.tag_id)
        .where(FeedbackGroupMember.group_id.in_(ids))
        .group_by(FeedbackGroupMember.group_id, Tag.id)
        .order_by(Tag.slug, Tag.id)
    )
    for group_id, slug, name, default, submission, admin in rows:
        result[group_id].append(
            {
                "slug": slug,
                "name": name,
                "provenance_counts": {
                    "from_default": default,
                    "from_submission": submission,
                    "from_admin": admin,
                },
            }
        )
    return result


def _delivery_summaries(db: Session, ids: list[str]) -> dict[str, dict[str, int]]:
    result = {identifier: {"delivered": 0, "in_progress": 0, "failed": 0} for identifier in ids}
    if ids:
        rows = db.execute(
            select(FeedbackGroupMember.group_id, Delivery.status, func.count())
            .join(Delivery, Delivery.event_id == FeedbackGroupMember.event_id)
            .where(
                FeedbackGroupMember.group_id.in_(ids),
                Delivery.status.in_(("delivered", "processing", "failed")),
            )
            .group_by(FeedbackGroupMember.group_id, Delivery.status)
        )
        for group_id, status, count in rows:
            result[group_id]["in_progress" if status == "processing" else status] = count
    return result


def _row(
    group: FeedbackGroup,
    project: Project,
    event: FeedbackEvent,
    tags: list[dict[str, Any]],
    summary: dict[str, int],
) -> dict[str, Any]:
    return {
        "id": group.id,
        "project": _project(project),
        "event_type": group.event_type,
        "representative": {"event_id": event.id, "question_excerpt": event.question[:280]},
        "tags": [{"slug": tag["slug"], "name": tag["name"]} for tag in tags[:20]],
        "tags_more": len(tags) > 20,
        "status": group.status,
        "occurrence_count": group.occurrence_count,
        "first_seen_at": _iso_utc(group.first_seen_at),
        "last_seen_at": _iso_utc(group.last_seen_at),
        "delivery_summary": summary,
    }


def install_admin_group_routes(app: Any) -> None:
    router = APIRouter(
        prefix="/api/v1/admin/groups", tags=["admin-groups"], dependencies=[Depends(_reject_bearer)]
    )

    @router.get("")
    def list_groups(
        request: Request,
        auth: AdminAuth,
        project_id: str | None = None,
        event_type: str | None = None,
        status: list[str] = STATUS_QUERY,
        tag: list[str] = TAG_QUERY,
        q: str | None = None,
        first_seen_from: datetime | None = None,
        first_seen_to: datetime | None = None,
        last_seen_from: datetime | None = None,
        last_seen_to: datetime | None = None,
        min_occurrences: int | None = None,
        sort: str = "last_activity_desc",
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
    ) -> Any:
        del auth
        if (
            (project_id is not None and _id(project_id) is None)
            or (
                event_type is not None
                and event_type not in {"knowledge_gap", "knowledge_correction"}
            )
            or any(item not in STATUSES for item in status)
            or len(tag) > 10
            or any(not item for item in tag)
            or sort not in SORTS
            or (q is not None and (len(q) > 200 or _fts(q) is None))
            or (min_occurrences is not None and min_occurrences < 1)
        ):
            return _bad(request)
        try:
            tag = [normalize_tag_v1(item) for item in tag]
        except ValueError:
            return _bad(request)
        first_seen_from = _utc(first_seen_from) if first_seen_from else None
        first_seen_to = _utc(first_seen_to) if first_seen_to else None
        last_seen_from = _utc(last_seen_from) if last_seen_from else None
        last_seen_to = _utc(last_seen_to) if last_seen_to else None
        filters = {
            "project_id": project_id,
            "event_type": event_type,
            "status": sorted(set(status)),
            "tag": sorted(set(tag)),
            "q": q,
            "first_seen_from": first_seen_from.isoformat() if first_seen_from else None,
            "first_seen_to": first_seen_to.isoformat() if first_seen_to else None,
            "last_seen_from": last_seen_from.isoformat() if last_seen_from else None,
            "last_seen_to": last_seen_to.isoformat() if last_seen_to else None,
            "min_occurrences": min_occurrences,
            "sort": sort,
        }
        digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        rank = case(
            *[(FeedbackGroup.status == value, index) for index, value in enumerate(STATUSES)],
            else_=4,
        )
        columns = {
            "last_seen_at": FeedbackGroup.last_seen_at,
            "first_seen_at": FeedbackGroup.first_seen_at,
            "occurrence_count": FeedbackGroup.occurrence_count,
            "status_rank": rank,
        }
        order = [(columns[name], desc) for name, desc in SORTS[sort]]
        statement = (
            select(FeedbackGroup, Project, FeedbackEvent)
            .join(Project, Project.id == FeedbackGroup.project_id)
            .join(FeedbackEvent, FeedbackEvent.id == FeedbackGroup.representative_event_id)
        )
        if project_id:
            statement = statement.where(FeedbackGroup.project_id == _id(project_id))
        if event_type:
            statement = statement.where(FeedbackGroup.event_type == event_type)
        if status:
            statement = statement.where(FeedbackGroup.status.in_(set(status)))
        if tag:
            tags = sorted(set(tag))
            matching = (
                select(FeedbackGroupMember.group_id)
                .join(FeedbackEventTag, FeedbackEventTag.event_id == FeedbackGroupMember.event_id)
                .join(Tag, Tag.id == FeedbackEventTag.tag_id)
                .where(Tag.slug.in_(tags))
                .group_by(FeedbackGroupMember.group_id)
                .having(func.count(distinct(Tag.slug)) == len(tags))
            )
            statement = statement.where(FeedbackGroup.id.in_(matching))
        if q is not None:
            matching_events = text(
                "SELECT event_id FROM feedback_search WHERE feedback_search MATCH :query"
            )
            matching_groups = select(FeedbackGroupMember.group_id).where(
                FeedbackGroupMember.event_id.in_(matching_events.bindparams(query=_fts(q)))
            )
            statement = statement.where(FeedbackGroup.id.in_(matching_groups))
        for field, lower, upper in (
            (FeedbackGroup.first_seen_at, first_seen_from, first_seen_to),
            (FeedbackGroup.last_seen_at, last_seen_from, last_seen_to),
        ):
            if lower:
                statement = statement.where(field >= lower)
            if upper:
                statement = statement.where(field <= upper)
        if min_occurrences is not None:
            statement = statement.where(FeedbackGroup.occurrence_count >= min_occurrences)
        if cursor is not None:
            point = _cursor_decode(cursor, digest, len(order))
            if point is None:
                return _bad(request, "invalid_cursor", 400)
            values, identifier = point
            try:
                parsed = [
                    _utc(datetime.fromisoformat(value)) if name.endswith("_at") else value
                    for (name, _), value in zip(SORTS[sort], values, strict=True)
                ]
                if any(
                    not isinstance(value, datetime | int) or isinstance(value, bool)
                    for value in parsed
                ):
                    return _bad(request, "invalid_cursor", 400)
            except (TypeError, ValueError, OverflowError):
                return _bad(request, "invalid_cursor", 400)
            prefix: list[ColumnElement[bool]] = []
            branches: list[ColumnElement[bool]] = []
            for (column, descending), value in zip(order, parsed, strict=True):
                branches.append(and_(*prefix, column < value if descending else column > value))
                prefix.append(column == value)
            branches.append(and_(*prefix, FeedbackGroup.id < identifier))
            statement = statement.where(or_(*branches))
        statement = statement.order_by(
            *[column.desc() if descending else column.asc() for column, descending in order],
            FeedbackGroup.id.desc(),
        )
        db: Session = request.state.db
        rows = db.execute(statement.limit(limit + 1)).all()
        page = rows[:limit]
        ids = [group.id for group, _, _ in page]
        tags_by_group = _tag_rows(db, ids)
        summaries = _delivery_summaries(db, ids)
        next_cursor = None
        if len(rows) > limit:
            last = page[-1][0]
            values = [
                getattr(last, name).isoformat()
                if name.endswith("_at")
                else STATUSES.index(last.status)
                if name == "status_rank"
                else getattr(last, name)
                for name, _ in SORTS[sort]
            ]
            next_cursor = _cursor_encode(values, last.id, digest)
        return {
            "items": [
                _row(group, project, event, tags_by_group[group.id], summaries[group.id])
                for group, project, event in page
            ],
            "next_cursor": next_cursor,
        }

    @router.get("/summary")
    def summary(request: Request, auth: AdminAuth, project_id: str | None = None) -> Any:
        del auth
        if project_id is not None and _id(project_id) is None:
            return _bad(request)
        db: Session = request.state.db
        counts = {value: 0 for value in STATUSES}
        totals = select(
            FeedbackGroup.status,
            func.count(),
            func.coalesce(func.sum(FeedbackGroup.occurrence_count), 0),
        ).group_by(FeedbackGroup.status)
        if project_id:
            totals = totals.where(FeedbackGroup.project_id == _id(project_id))
        total_groups = total_events = 0
        for status, group_count, event_count in db.execute(totals):
            counts[status] = group_count
            total_groups += group_count
            total_events += event_count
        failed = (
            select(func.count())
            .select_from(Delivery)
            .where(
                Delivery.status == "failed",
                Delivery.updated_at >= datetime.now(UTC) - timedelta(hours=24),
            )
        )
        if project_id:
            failed = failed.where(Delivery.project_id == _id(project_id))
        top_query = select(FeedbackGroup).where(
            FeedbackGroup.status == "open", FeedbackGroup.event_type == "knowledge_gap"
        )
        if project_id:
            top_query = top_query.where(FeedbackGroup.project_id == _id(project_id))
        top = db.scalars(
            top_query.order_by(
                FeedbackGroup.occurrence_count.desc(),
                FeedbackGroup.last_seen_at.desc(),
                FeedbackGroup.id.asc(),
            ).limit(5)
        ).all()
        projects = {
            project.id: project
            for project in db.scalars(
                select(Project).where(Project.id.in_([g.project_id for g in top]))
            )
        }
        events = {
            event.id: event
            for event in db.scalars(
                select(FeedbackEvent).where(
                    FeedbackEvent.id.in_([g.representative_event_id for g in top])
                )
            )
        }
        tags = _tag_rows(db, [group.id for group in top])
        deliveries = _delivery_summaries(db, [group.id for group in top])
        return {
            "total_groups": total_groups,
            "total_events": total_events,
            "groups_by_status": counts,
            "failed_deliveries_last_24h": db.scalar(failed),
            "top_open_gaps": [
                _row(
                    group,
                    projects[group.project_id],
                    events[group.representative_event_id],
                    tags[group.id],
                    deliveries[group.id],
                )
                for group in top
            ],
        }

    @router.get("/{group_id}")
    def detail(group_id: str, request: Request, auth: AdminAuth) -> Any:
        del auth
        db: Session = request.state.db
        group = _group(db, group_id)
        if group is None:
            return error_response(404, "not_found", "Group not found", request.state.request_id)
        project = db.get(Project, group.project_id)
        event = db.get(FeedbackEvent, group.representative_event_id)
        assert project is not None and event is not None
        tags = _tag_rows(db, [group.id])[group.id]
        notes = db.scalars(
            select(FeedbackGroupNote)
            .where(FeedbackGroupNote.group_id == group.id)
            .order_by(FeedbackGroupNote.created_at.desc(), FeedbackGroupNote.id.desc())
        ).all()
        return {
            **_row(group, project, event, tags, _delivery_summaries(db, [group.id])[group.id]),
            "representative": {
                "event_id": event.id,
                "question": event.question,
                "question_excerpt": event.question[:280],
            },
            "tags": tags,
            "tags_more": False,
            "status_changed_at": _iso_utc(group.status_changed_at),
            "resolved_at": _iso_utc(group.resolved_at) if group.resolved_at else None,
            "notes": [
                {
                    "id": note.id,
                    "body": note.body,
                    "author_admin_id": note.author_admin_id,
                    "created_at": _iso_utc(note.created_at),
                }
                for note in notes
            ],
        }

    @router.get("/{group_id}/events")
    def events(group_id: str, request: Request, auth: AdminAuth, cursor: str | None = None) -> Any:
        del auth
        db: Session = request.state.db
        group = _group(db, group_id)
        if group is None:
            return error_response(404, "not_found", "Group not found", request.state.request_id)
        statement = (
            select(FeedbackEvent)
            .join(FeedbackGroupMember, FeedbackGroupMember.event_id == FeedbackEvent.id)
            .where(FeedbackGroupMember.group_id == group.id)
        )
        if cursor is not None:
            point = _cursor_decode(cursor, group.id, 1)
            if point is None:
                return _bad(request, "invalid_cursor", 400)
            try:
                at = datetime.fromisoformat(point[0][0])
            except (TypeError, ValueError):
                return _bad(request, "invalid_cursor", 400)
            statement = statement.where(
                or_(
                    FeedbackEvent.created_at > at,
                    and_(FeedbackEvent.created_at == at, FeedbackEvent.id > point[1]),
                )
            )
        rows = db.scalars(
            statement.order_by(FeedbackEvent.created_at, FeedbackEvent.id).limit(21)
        ).all()
        page = rows[:20]
        items = []
        for event in page:
            item = serialize_event_detail(db, event)
            member = db.get(FeedbackGroupMember, event.id)
            assert member is not None
            item["match"] = {
                "method": member.match_method,
                "triggered_reopen": member.triggered_reopen,
            }
            evaluation = db.get(DedupEvaluation, event.id)
            item["semantic"] = (
                {
                    "outcome": evaluation.outcome,
                    "confidence": evaluation.confidence,
                    "reason": evaluation.reason,
                    "error_category": evaluation.error_category,
                }
                if evaluation is not None
                else None
            )
            items.append(item)
        return {
            "items": items,
            "next_cursor": _cursor_encode([page[-1].created_at.isoformat()], page[-1].id, group.id)
            if len(rows) > 20
            else None,
        }

    @router.get("/{group_id}/history")
    def history(group_id: str, request: Request, auth: AdminAuth, cursor: str | None = None) -> Any:
        del auth
        db: Session = request.state.db
        group = _group(db, group_id)
        if group is None:
            return error_response(404, "not_found", "Group not found", request.state.request_id)
        statement = select(FeedbackGroupHistory).where(FeedbackGroupHistory.group_id == group.id)
        if cursor is not None:
            point = _cursor_decode(cursor, group.id, 1)
            if point is None:
                return _bad(request, "invalid_cursor", 400)
            try:
                at = datetime.fromisoformat(point[0][0])
            except (TypeError, ValueError):
                return _bad(request, "invalid_cursor", 400)
            statement = statement.where(
                or_(
                    FeedbackGroupHistory.created_at < at,
                    and_(FeedbackGroupHistory.created_at == at, FeedbackGroupHistory.id < point[1]),
                )
            )
        rows = db.scalars(
            statement.order_by(
                FeedbackGroupHistory.created_at.desc(), FeedbackGroupHistory.id.desc()
            ).limit(51)
        ).all()
        page = rows[:50]
        return {
            "items": [
                {
                    "id": item.id,
                    "action": item.action,
                    "actor_type": item.actor_type,
                    "actor_admin_id": item.actor_admin_id,
                    "origin_group_id": item.origin_group_id,
                    "from_status": item.from_status,
                    "to_status": item.to_status,
                    "details": json.loads(item.details_json),
                    "created_at": _iso_utc(item.created_at),
                }
                for item in page
            ],
            "next_cursor": _cursor_encode([page[-1].created_at.isoformat()], page[-1].id, group.id)
            if len(rows) > 50
            else None,
        }

    app.include_router(router)
