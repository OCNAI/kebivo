"""Durable feedback acceptance shared by REST and future MCP callers."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kebivo.dedup.normalize import question_hash
from kebivo.feedback.groups import associate, create_initial_group
from kebivo.feedback.schemas import FeedbackSubmission
from kebivo.feedback.tags import display_tag_v1, normalize_tag_v1
from kebivo.models import (
    FeedbackEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupMember,
    IdempotencyRecord,
    ProcessingJob,
    Project,
    Tag,
    now_utc,
)
from kebivo.tokens import ProjectTokenPrincipal, touch_project_token


@dataclass(frozen=True)
class SubmissionResult:
    event_id: str
    processing_status: str
    replayed: bool = False


class IdempotencyConflict(Exception):
    pass


def validate_idempotency_key(key: str | None) -> str | None:
    if key is not None and (not 1 <= len(key) <= 255 or any(not 33 <= ord(c) <= 126 for c in key)):
        raise ValueError("Invalid idempotency key")
    return key


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def submission_digest(submission: FeedbackSubmission) -> str:
    fields = submission.model_dump(exclude_none=True, exclude_defaults=True)
    fields["event_type"] = submission.event_type
    fields["tags"] = sorted({normalize_tag_v1(tag) for tag in submission.tags})
    fields["metadata"] = submission.metadata
    return hashlib.sha256(_json(fields).encode("utf-8")).hexdigest()


def normalized_question_hash(project_id: str, event_type: str, question: str) -> str:
    return question_hash(project_id, event_type, question)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _existing(
    db: Session, project_id: str, key: str, digest: str, now: datetime
) -> tuple[IdempotencyRecord | None, SubmissionResult | None]:
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.project_id == project_id, IdempotencyRecord.key == key
        )
    )
    if record is None or _utc(record.expires_at) <= now:
        return record, None
    if record.payload_digest != digest:
        raise IdempotencyConflict
    event = db.get(FeedbackEvent, record.event_id)
    return record, SubmissionResult(event.id, event.processing_status, True) if event else None


def submit_feedback(
    db: Session,
    principal: ProjectTokenPrincipal,
    submission: FeedbackSubmission,
    idempotency_key: str | None,
    *,
    window_hours: int = 24,
    submitted_via: str = "rest",
) -> SubmissionResult:
    """Commit an event, its group and pending job atomically."""
    key = validate_idempotency_key(idempotency_key)
    digest = submission_digest(submission)
    now = now_utc()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        record = None
        if key is not None:
            record, replay = _existing(db, principal.project_id, key, digest, now)
            if replay is not None:
                db.commit()
                return replay
        submitted = {normalize_tag_v1(tag): tag for tag in reversed(submission.tags)}
        defaults = db.scalars(
            select(Tag).where(Tag.project_id == principal.project_id, Tag.is_default.is_(True))
        ).all()
        effective: dict[str, tuple[Tag, bool, bool]] = {
            tag.slug: (tag, True, tag.slug in submitted) for tag in defaults
        }
        submitted_only = [slug for slug in submitted if slug not in effective]
        existing: dict[str, Tag] = {}
        for start in range(0, len(submitted_only), 500):
            tags = db.scalars(
                select(Tag).where(
                    Tag.project_id == principal.project_id,
                    Tag.slug.in_(submitted_only[start : start + 500]),
                )
            ).all()
            existing.update((tag.slug, tag) for tag in tags)
        new_tags: list[Tag] = []
        for slug in submitted_only:
            tag = existing.get(slug)
            if tag is None:
                tag = Tag(
                    project_id=principal.project_id,
                    name=display_tag_v1(submitted[slug]),
                    slug=slug,
                    is_default=False,
                )
                new_tags.append(tag)
            effective[slug] = (tag, False, True)
        event = FeedbackEvent(
            project_id=principal.project_id,
            api_token_id=principal.token_id,
            submitted_via=submitted_via,
            event_type=submission.event_type,
            question=submission.question,
            correction=getattr(submission, "correction", None),
            previous_answer=submission.previous_answer,
            context=submission.context,
            source_reference=getattr(submission, "source_reference", None),
            agent_id=submission.agent_id,
            conversation_id=submission.conversation_id,
            source_event_id=submission.source_event_id,
            user_reference=submission.user_reference,
            metadata_json=_json(submission.metadata),
            normalized_question_hash=normalized_question_hash(
                principal.project_id, submission.event_type, submission.question
            ),
            question_hash_version=2,
            idempotency_key=key,
            processing_status="pending",
        )
        db.add_all([*new_tags, event])
        db.flush()
        project = db.get(Project, principal.project_id)
        assert project is not None
        mode = json.loads(project.settings_json).get("deduplication", {}).get("mode", "disabled")
        prior = None
        if mode in {"exact", "semantic"}:
            prior = db.scalar(
                select(FeedbackEvent)
                .where(
                    FeedbackEvent.project_id == event.project_id,
                    FeedbackEvent.event_type == event.event_type,
                    FeedbackEvent.normalized_question_hash == event.normalized_question_hash,
                    FeedbackEvent.id != event.id,
                )
                .order_by(FeedbackEvent.created_at.desc(), FeedbackEvent.id.desc())
                .limit(1)
            )
        if prior is None:
            create_initial_group(db, event)
        else:
            group_id = db.scalar(
                select(FeedbackGroupMember.group_id).where(FeedbackGroupMember.event_id == prior.id)
            )
            group = db.get(FeedbackGroup, group_id)
            assert group is not None
            associate(db, event, group, "exact")
        db.add_all(
            [
                FeedbackEventTag(
                    event_id=event.id,
                    tag_id=tag.id,
                    from_default=from_default,
                    from_submission=from_submission,
                )
                for tag, from_default, from_submission in effective.values()
            ]
        )
        db.add(ProcessingJob(event_id=event.id, status="pending"))
        if key is not None:
            if record is None:
                db.add(
                    IdempotencyRecord(
                        project_id=principal.project_id,
                        key=key,
                        payload_digest=digest,
                        event_id=event.id,
                        created_at=now,
                        expires_at=now + timedelta(hours=window_hours),
                    )
                )
            else:
                record.payload_digest = digest
                record.event_id = event.id
                record.created_at = now
                record.expires_at = now + timedelta(hours=window_hours)
        touch_project_token(db, principal.token_id, now=now)
        db.commit()
        return SubmissionResult(event.id, "pending")
    except IntegrityError:
        db.rollback()
        if key is not None:
            _, replay = _existing(db, principal.project_id, key, digest, now_utc())
            if replay is not None:
                return replay
        raise
    except Exception:
        db.rollback()
        raise
