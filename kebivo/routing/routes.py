"""Admin routing rule API and read-only simulator."""

import json
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.feedback.tags import display_tag_v1, normalize_tag_v1
from kebivo.models import (
    AuditEvent,
    Destination,
    FeedbackGroup,
    Project,
    RoutingRule,
    RoutingRuleDestination,
    RoutingRuleTag,
    Tag,
    now_utc,
)
from kebivo.routing.coverage import delivered_destinations, has_unrouted_earlier_member
from kebivo.routing.engine import DestinationInput, RuleInput, evaluate


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    event_types: list[str] = Field(min_length=1)
    match_mode: str = "any"
    tags: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(ge=0, le=1_000_000)
    destination_ids: list[str] = Field(min_length=1, max_length=10)
    match_behavior: str = "continue"


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    enabled: bool | None = None
    event_types: list[str] | None = None
    match_mode: str | None = None
    tags: list[str] | None = None
    priority: int | None = None
    destination_ids: list[str] | None = None
    match_behavior: str | None = None


class Simulation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str
    tags: list[str] = Field(default_factory=list, max_length=20)
    include_default_tags: bool = True
    group_id: str | None = None


def _fail(
    request: Request, status: int, code: str, message: str, field: str | None = None
) -> Response:
    details = {"fields": [{"field": field, "code": code, "message": message}]} if field else None
    return error_response(status, code, message, request.state.request_id, details)


def _uuid(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _project(db: Session, value: str) -> Project | None:
    return db.get(Project, _uuid(value))


def _related(db: Session, row: RoutingRule) -> tuple[list[Tag], list[Destination]]:
    tags = db.scalars(
        select(Tag)
        .join(RoutingRuleTag, RoutingRuleTag.tag_id == Tag.id)
        .where(RoutingRuleTag.rule_id == row.id)
        .order_by(Tag.slug)
    ).all()
    destinations = db.scalars(
        select(Destination)
        .join(RoutingRuleDestination, RoutingRuleDestination.destination_id == Destination.id)
        .where(RoutingRuleDestination.rule_id == row.id)
        .order_by(Destination.id)
    ).all()
    return list(tags), list(destinations)


def _serialize(db: Session, row: RoutingRule) -> dict[str, Any]:
    tags, destinations = _related(db, row)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": row.name,
        "enabled": row.enabled,
        "event_types": json.loads(row.event_types_json),
        "match_mode": cast(Literal["any", "all"], row.match_mode),
        "tags": [tag.slug for tag in tags],
        "priority": row.priority,
        "destination_ids": [dest.id for dest in destinations],
        "match_behavior": cast(Literal["continue", "stop"], row.match_behavior),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _check(payload: dict[str, Any], db: Session) -> tuple[list[str], list[str]]:
    if not isinstance(payload.get("name"), str) or not 1 <= len(payload["name"].strip()) <= 120:
        raise ValueError("name")
    events = payload.get("event_types")
    if (
        not isinstance(events, list)
        or not events
        or set(events) - {"knowledge_gap", "knowledge_correction"}
    ):
        raise ValueError("event_types")
    if payload.get("match_mode") not in {"any", "all"}:
        raise ValueError("match_mode")
    if payload.get("match_behavior") not in {"continue", "stop"}:
        raise ValueError("match_behavior")
    priority = payload.get("priority")
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not 0 <= priority <= 1_000_000
    ):
        raise ValueError("priority")
    names = payload.get("tags")
    if not isinstance(names, list) or len(names) > 20:
        raise ValueError("tags")
    try:
        tags = list(dict.fromkeys(normalize_tag_v1(name) for name in names))
        for name in names:
            display_tag_v1(name)
    except ValueError:
        raise ValueError("tags") from None
    ids = payload.get("destination_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids):
        raise ValueError("destination_ids")
    if any(_uuid(item) != item or db.get(Destination, item) is None for item in ids):
        raise ValueError("destination_ids")
    return tags, ids


def _save_relations(
    db: Session,
    row: RoutingRule,
    project_id: str,
    tags: list[str],
    ids: list[str],
    display_names: list[str],
) -> None:
    db.execute(delete(RoutingRuleTag).where(RoutingRuleTag.rule_id == row.id))
    db.execute(delete(RoutingRuleDestination).where(RoutingRuleDestination.rule_id == row.id))
    existing = {
        tag.slug: tag for tag in db.scalars(select(Tag).where(Tag.project_id == project_id))
    }
    names = {normalize_tag_v1(name): display_tag_v1(name) for name in display_names}
    for slug in tags:
        tag = existing.get(slug)
        if tag is None:
            tag = Tag(project_id=project_id, slug=slug, name=names[slug])
            db.add(tag)
            db.flush()
        db.add(RoutingRuleTag(rule_id=row.id, tag_id=tag.id))
    for destination_id in ids:
        db.add(RoutingRuleDestination(rule_id=row.id, destination_id=destination_id))


def _audit(db: Session, admin_id: str, action: str, row: RoutingRule) -> None:
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin_id,
            action=action,
            subject_type="routing_rule",
            subject_id=row.id,
            details_json="{}",
        )
    )


def install_routing_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["routing"])

    @router.get("/routing-rules")
    def list_rules(project_id: str, request: Request, auth: AdminAuth) -> Any:
        del auth
        db: Session = request.state.db
        if _project(db, project_id) is None:
            return _fail(request, 404, "project_not_found", "Project not found")
        rows = db.scalars(
            select(RoutingRule)
            .where(RoutingRule.project_id == project_id)
            .order_by(RoutingRule.priority, RoutingRule.created_at, RoutingRule.id)
        ).all()
        return {"items": [_serialize(db, row) for row in rows]}

    @router.post("/routing-rules", status_code=201)
    def create_rule(project_id: str, body: RuleBody, request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _project(db, project_id)
        if project is None:
            return _fail(request, 404, "project_not_found", "Project not found")
        if project.archived_at is not None:
            return _fail(request, 409, "project_archived", "Archived projects cannot be edited")
        if (
            db.scalar(
                select(func.count())
                .select_from(RoutingRule)
                .where(RoutingRule.project_id == project.id)
            )
            or 0
        ) >= 100:
            return _fail(request, 409, "rule_limit", "Project has 100 routing rules")
        payload = body.model_dump()
        try:
            tags, ids = _check(payload, db)
        except ValueError as exc:
            return _fail(request, 422, "validation_error", "Invalid routing rule", str(exc))
        row = RoutingRule(
            project_id=project.id,
            name=payload["name"].strip(),
            enabled=payload["enabled"],
            event_types_json=json.dumps(
                list(dict.fromkeys(payload["event_types"])), separators=(",", ":")
            ),
            match_mode=payload["match_mode"],
            priority=payload["priority"],
            match_behavior=payload["match_behavior"],
        )
        db.add(row)
        db.flush()
        _save_relations(db, row, project.id, tags, ids, payload["tags"])
        _audit(db, admin.id, "routing_rule.created", row)
        db.commit()
        return _serialize(db, row)

    @router.patch("/routing-rules/{rule_id}")
    def patch_rule(
        project_id: str, rule_id: str, body: RulePatch, request: Request, auth: AdminAuth
    ) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _project(db, project_id)
        row = db.get(RoutingRule, _uuid(rule_id))
        if project is None or row is None or row.project_id != project.id:
            return _fail(request, 404, "rule_not_found", "Routing rule not found")
        if project.archived_at is not None:
            return _fail(request, 409, "project_archived", "Archived projects cannot be edited")
        payload = {
            key: value for key, value in _serialize(db, row).items() if key in RuleBody.model_fields
        }
        changes = body.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            field = next(key for key, value in changes.items() if value is None)
            return _fail(request, 422, "validation_error", "Invalid routing rule", field)
        payload.update(changes)
        try:
            tags, ids = _check(payload, db)
        except ValueError as exc:
            return _fail(request, 422, "validation_error", "Invalid routing rule", str(exc))
        row.name = payload["name"].strip()
        row.enabled = payload["enabled"]
        row.event_types_json = json.dumps(
            list(dict.fromkeys(payload["event_types"])), separators=(",", ":")
        )
        row.match_mode = payload["match_mode"]
        row.priority = payload["priority"]
        row.match_behavior = payload["match_behavior"]
        row.updated_at = now_utc()
        _save_relations(db, row, project.id, tags, ids, payload["tags"])
        _audit(db, admin.id, "routing_rule.updated", row)
        db.commit()
        return _serialize(db, row)

    @router.delete("/routing-rules/{rule_id}", status_code=204, response_class=Response)
    def delete_rule(project_id: str, rule_id: str, request: Request, auth: AdminAuth) -> Response:
        admin, _, _ = auth
        db: Session = request.state.db
        row = db.get(RoutingRule, _uuid(rule_id))
        if row is None or row.project_id != _uuid(project_id):
            return _fail(request, 404, "rule_not_found", "Routing rule not found")
        _audit(db, admin.id, "routing_rule.deleted", row)
        db.delete(row)
        db.commit()
        return Response(status_code=204)

    @router.post("/routing/simulate")
    def simulate(project_id: str, body: Simulation, request: Request, auth: AdminAuth) -> Any:
        del auth
        db: Session = request.state.db
        project = _project(db, project_id)
        if project is None:
            return _fail(request, 404, "project_not_found", "Project not found")
        if body.event_type not in {"knowledge_gap", "knowledge_correction"}:
            return _fail(request, 422, "validation_error", "Invalid event type", "event_type")
        try:
            tags = {normalize_tag_v1(name) for name in body.tags}
        except ValueError:
            return _fail(request, 422, "validation_error", "Invalid tag", "tags")
        if body.include_default_tags:
            tags.update(
                db.scalars(
                    select(Tag.slug).where(Tag.project_id == project.id, Tag.is_default.is_(True))
                )
            )
        rows = db.scalars(select(RoutingRule).where(RoutingRule.project_id == project.id)).all()
        rules = []
        for row in rows:
            related_tags, related_destinations = _related(db, row)
            rules.append(
                RuleInput(
                    row.id,
                    row.name,
                    row.priority,
                    row.created_at,
                    row.enabled,
                    tuple(json.loads(row.event_types_json)),
                    cast(Literal["any", "all"], row.match_mode),
                    tuple(tag.slug for tag in related_tags),
                    tuple(dest.id for dest in related_destinations),
                    cast(Literal["continue", "stop"], row.match_behavior),
                )
            )
        initial = True
        previously_delivered: frozenset[str] = frozenset()
        if body.group_id is not None:
            group = db.get(FeedbackGroup, _uuid(body.group_id))
            if (
                group is None
                or group.project_id != project.id
                or group.event_type != body.event_type
            ):
                return _fail(request, 422, "validation_error", "Invalid group", "group_id")
            initial = False
            covered = delivered_destinations(db, group.id)
            if has_unrouted_earlier_member(db, group.id):
                covered.update(row.id for row in db.scalars(select(Destination)))
            previously_delivered = frozenset(covered)
        destinations = [
            DestinationInput(
                row.id,
                row.enabled,
                cast(Literal["new_groups_only", "every_occurrence"], row.delivery_mode),
            )
            for row in db.scalars(select(Destination))
        ]
        result = evaluate(
            body.event_type,
            tags,
            rules,
            destinations,
            project.fallback_destination_id,
            initial=initial,
            previously_delivered=previously_delivered,
        )
        return {
            "evaluated_rules": [item.__dict__ for item in result.evaluated_rules],
            "stopped_by_rule_id": result.stopped_by_rule_id,
            "destinations": [item.__dict__ for item in result.destinations],
            "fallback_applied": result.fallback_applied,
            "warnings": result.warnings,
        }

    app.include_router(router)
