"""Admin-only, removable sample rows for a fresh installation."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.feedback.service import normalized_question_hash
from kebivo.models import (
    ApiToken,
    AuditEvent,
    DedupEvaluation,
    Delivery,
    DeliveryAttempt,
    Destination,
    EmbeddingBackfill,
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    IdempotencyRecord,
    ProcessingJob,
    Project,
    QuestionEmbedding,
    RoutingMatch,
    RoutingRule,
    RoutingRuleDestination,
    RoutingRuleTag,
    Tag,
    now_utc,
)


def _state(db: Session) -> dict[str, bool]:
    sample_projects = select(Project.id).where(Project.is_sample.is_(True))
    return {
        "present": db.scalar(sample_projects.limit(1)) is not None
        or db.scalar(select(Destination.id).where(Destination.is_sample.is_(True)).limit(1))
        is not None,
        "has_real_events": db.scalar(
            select(FeedbackEvent.id).join(Project).where(Project.is_sample.is_(False)).limit(1)
        )
        is not None,
        "has_real_projects": db.scalar(
            select(Project.id)
            .where(Project.is_sample.is_(False), Project.archived_at.is_(None))
            .limit(1)
        )
        is not None,
    }


def _load(db: Session) -> dict[str, int]:
    fixture = json.loads(Path(__file__).with_name("sample_data.json").read_text())
    now = now_utc()
    totals = {"projects": 0, "groups": 0, "events": 0, "deliveries": 0}
    for spec in fixture["projects"]:
        project = Project(
            name=spec["name"],
            slug=spec["slug"],
            description=spec["description"],
            is_sample=True,
            created_at=now,
        )
        db.add(project)
        db.flush()
        totals["projects"] += 1
        destinations = {}
        for item in spec["destinations"]:
            destination = Destination(
                name=item["name"],
                type="webhook",
                is_sample=True,
                config_json=json.dumps(
                    {
                        "url": item["url"],
                        "auth": {"type": "none"},
                        "signing": False,
                        "timeout_seconds": 10,
                    },
                    separators=(",", ":"),
                ),
                created_at=now,
            )
            db.add(destination)
            db.flush()
            destinations[item["name"]] = destination
        tags = {name for group in spec["groups"] for name in group["tags"]}
        tags.update(rule["tag"] for rule in spec["rules"])
        tag_rows = {}
        for name in sorted(tags):
            tag = Tag(project_id=project.id, name=name, slug=name)
            db.add(tag)
            db.flush()
            tag_rows[name] = tag
        rules = {}
        for rule_spec in spec["rules"]:
            rule = RoutingRule(
                project_id=project.id,
                name=rule_spec["name"],
                priority=rule_spec["priority"],
                event_types_json='["knowledge_gap"]',
                match_mode="any",
                match_behavior="continue",
            )
            db.add(rule)
            db.flush()
            rules[rule_spec["tag"]] = (rule, destinations[rule_spec["destination"]])
            db.add_all(
                [
                    RoutingRuleTag(rule_id=rule.id, tag_id=tag_rows[rule_spec["tag"]].id),
                    RoutingRuleDestination(
                        rule_id=rule.id, destination_id=destinations[rule_spec["destination"]].id
                    ),
                ]
            )
        for group_spec in spec["groups"]:
            count = group_spec["count"]
            first = now - timedelta(days=group_spec["days_ago"])
            last = now - timedelta(days=group_spec.get("last_days_ago", group_spec["days_ago"]))
            events = []
            for index in range(count):
                when = first + (last - first) * index / max(1, count - 1)
                event = FeedbackEvent(
                    project_id=project.id,
                    submitted_via="rest",
                    event_type="knowledge_gap",
                    question=group_spec["question"],
                    normalized_question_hash=normalized_question_hash(
                        project.id, "knowledge_gap", group_spec["question"]
                    ),
                    question_hash_version=2,
                    processing_status="completed",
                    created_at=when,
                    updated_at=when,
                )
                db.add(event)
                db.flush()
                events.append(event)
                totals["events"] += 1
                for name in group_spec["tags"]:
                    db.add(
                        FeedbackEventTag(
                            event_id=event.id, tag_id=tag_rows[name].id, from_submission=True
                        )
                    )
                for name, (rule, destination) in rules.items():
                    if name in group_spec["tags"]:
                        db.add(
                            RoutingMatch(
                                event_id=event.id,
                                rule_id=rule.id,
                                rule_name=rule.name,
                                rule_order=rule.priority,
                                via="rule",
                                destination_id=destination.id,
                                outcome="delivery_created"
                                if index == 0
                                else "suppressed_duplicate",
                                created_at=when,
                            )
                        )
            status = group_spec["status"]
            group = FeedbackGroup(
                project_id=project.id,
                event_type="knowledge_gap",
                representative_event_id=events[0].id,
                status=status,
                occurrence_count=count,
                first_seen_at=events[0].created_at,
                last_seen_at=events[-1].created_at,
                created_at=events[0].created_at,
                updated_at=events[-1].created_at,
                status_changed_at=events[-1].created_at,
                resolved_at=events[-1].created_at if status == "resolved" else None,
            )
            db.add(group)
            db.flush()
            totals["groups"] += 1
            db.add(
                FeedbackGroupHistory(
                    group_id=group.id,
                    action="created",
                    actor_type="system",
                    created_at=events[0].created_at,
                )
            )
            if status != "open":
                db.add(
                    FeedbackGroupHistory(
                        group_id=group.id,
                        action="status_changed",
                        actor_type="admin",
                        from_status="open",
                        to_status=status,
                        created_at=events[-1].created_at,
                    )
                )
            for index, event in enumerate(events):
                db.add(
                    FeedbackGroupMember(
                        event_id=event.id,
                        group_id=group.id,
                        match_method="initial" if index == 0 else "exact",
                        added_at=event.created_at,
                    )
                )
            for note in group_spec["notes"]:
                db.add(
                    FeedbackGroupNote(
                        group_id=group.id, body=note, created_at=events[-1].created_at
                    )
                )
            if group_spec["delivery"]:
                matching = next(name for name in rules if name in group_spec["tags"])
                destination = rules[matching][1]
                failed = group_spec["delivery"] == "failed"
                event = events[0]
                delivery = Delivery(
                    event_id=event.id,
                    project_id=project.id,
                    destination_id=destination.id,
                    status="failed" if failed else "delivered",
                    attempt_count=1,
                    max_attempts=1 if failed else 6,
                    last_attempt_at=event.created_at,
                    created_at=event.created_at,
                    delivered_at=None if failed else event.created_at,
                    last_error_category="http_5xx" if failed else None,
                    last_error_message="Destination temporarily rejected delivery"
                    if failed
                    else None,
                )
                db.add(delivery)
                db.flush()
                db.add(
                    DeliveryAttempt(
                        delivery_id=delivery.id,
                        attempt_number=1,
                        trigger="auto",
                        started_at=event.created_at,
                        completed_at=event.created_at,
                        result="retryable_failure" if failed else "success",
                        error_category="http_5xx" if failed else None,
                        sanitized_error="Destination temporarily rejected delivery"
                        if failed
                        else None,
                    )
                )
                totals["deliveries"] += 1
    return totals


def _remove(db: Session) -> None:
    project_ids = select(Project.id).where(Project.is_sample.is_(True))
    destination_ids = select(Destination.id).where(Destination.is_sample.is_(True))
    event_ids = select(FeedbackEvent.id).where(FeedbackEvent.project_id.in_(project_ids))
    group_ids = select(FeedbackGroup.id).where(FeedbackGroup.project_id.in_(project_ids))
    rule_ids = select(RoutingRule.id).where(RoutingRule.project_id.in_(project_ids))
    delivery_ids = select(Delivery.id).where(Delivery.project_id.in_(project_ids))
    tag_ids = select(Tag.id).where(Tag.project_id.in_(project_ids))
    for model, condition in (
        (DeliveryAttempt, DeliveryAttempt.delivery_id.in_(delivery_ids)),
        (RoutingMatch, RoutingMatch.event_id.in_(event_ids)),
        (Delivery, Delivery.project_id.in_(project_ids)),
        (DedupEvaluation, DedupEvaluation.project_id.in_(project_ids)),
        (QuestionEmbedding, QuestionEmbedding.event_id.in_(event_ids)),
        (EmbeddingBackfill, EmbeddingBackfill.project_id.in_(project_ids)),
        (ProcessingJob, ProcessingJob.event_id.in_(event_ids)),
        (IdempotencyRecord, IdempotencyRecord.project_id.in_(project_ids)),
        (FeedbackEventTag, FeedbackEventTag.event_id.in_(event_ids)),
        (FeedbackGroupHistory, FeedbackGroupHistory.group_id.in_(group_ids)),
        (FeedbackGroupNote, FeedbackGroupNote.group_id.in_(group_ids)),
        (FeedbackGroupMember, FeedbackGroupMember.group_id.in_(group_ids)),
        (FeedbackGroup, FeedbackGroup.id.in_(group_ids)),
        (FeedbackEvent, FeedbackEvent.id.in_(event_ids)),
        (RoutingRuleTag, RoutingRuleTag.rule_id.in_(rule_ids)),
        (RoutingRuleDestination, RoutingRuleDestination.rule_id.in_(rule_ids)),
        (RoutingRule, RoutingRule.id.in_(rule_ids)),
        (ApiToken, ApiToken.project_id.in_(project_ids)),
        (Tag, Tag.id.in_(tag_ids)),
        (Project, Project.id.in_(project_ids)),
        (Destination, Destination.id.in_(destination_ids)),
    ):
        db.execute(delete(model).where(condition))


def install_sample_data_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/admin/sample-data", tags=["sample data"])

    @router.get("")
    def status(request: Request, auth: AdminAuth) -> dict[str, bool]:
        del auth
        return _state(request.state.db)

    @router.post("", status_code=201)
    def load(request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        state = _state(db)
        if state["present"] or state["has_real_events"]:
            db.rollback()
            return error_response(
                409,
                "sample_data_conflict",
                "Sample data cannot be loaded",
                request.state.request_id,
            )
        try:
            totals = _load(db)
            db.add(
                AuditEvent(
                    actor_type="admin",
                    actor_ref=admin.id,
                    action="sample_data.loaded",
                    subject_type="sample_data",
                    details_json=json.dumps(totals),
                )
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            return error_response(
                409,
                "sample_data_conflict",
                "Sample data conflicts with existing rows",
                request.state.request_id,
            )
        return totals

    @router.delete("", status_code=204)
    def remove(request: Request, auth: AdminAuth) -> Response:
        admin, _, _ = auth
        db: Session = request.state.db
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if _state(db)["present"]:
            _remove(db)
            db.add(
                AuditEvent(
                    actor_type="admin",
                    actor_ref=admin.id,
                    action="sample_data.removed",
                    subject_type="sample_data",
                    details_json="{}",
                )
            )
        db.commit()
        return Response(status_code=204)

    app.include_router(router)
