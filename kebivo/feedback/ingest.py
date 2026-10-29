"""Protocol-neutral feedback validation and durable acceptance."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from kebivo.feedback.schemas import CorrectionSubmission, FeedbackSubmission, GapSubmission
from kebivo.feedback.service import (
    IdempotencyConflict,
    SubmissionResult,
    submit_feedback,
    validate_idempotency_key,
)
from kebivo.feedback_limits import FixedWindowLimiter
from kebivo.models import Project
from kebivo.tokens import ProjectTokenPrincipal, authorize_feedback


@dataclass(frozen=True)
class NotAuthorized(Exception):
    code: str


@dataclass(frozen=True)
class InvalidSubmission(Exception):
    fields: list[Any]


class InvalidIdempotencyKey(Exception):
    pass


@dataclass(frozen=True)
class RateLimited(Exception):
    retry_after: int


class StorageBusy(Exception):
    """The feedback transaction could not acquire SQLite's writer lock."""


def _sqlite_busy(exc: OperationalError) -> bool:
    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    return (
        (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        if code
        else str(original).lower() in {"database is locked", "database table is locked"}
    )


async def ingest(
    session_factory: sessionmaker[Session],
    settings: Any,
    limiter: FixedWindowLimiter,
    principal: ProjectTokenPrincipal,
    payload: dict[str, Any],
    idempotency_key: str | None,
    *,
    submitted_via: str,
    client_ip: str,
    apply_ip_limit: bool = True,
    _submit: Callable[..., SubmissionResult] = submit_feedback,
) -> SubmissionResult:
    """Authorize, validate, rate-limit, then commit one feedback event."""

    def load_project() -> Project | None:
        with session_factory() as db:
            return db.get(Project, principal.project_id)

    project = await run_in_threadpool(load_project)
    if project is None or not project.is_active or project.archived_at is not None:
        raise NotAuthorized("unauthorized")
    event_type = payload.get("event_type")
    if not isinstance(event_type, str) or event_type not in {
        "knowledge_gap",
        "knowledge_correction",
    }:
        raise InvalidSubmission([{"loc": ("event_type",), "type": "literal_error"}])
    try:
        authorize_feedback(principal, project, event_type)
    except HTTPException as exc:
        raise NotAuthorized(
            "scope_required"
            if exc.detail == "Project token scope required"
            else "corrections_disabled"
        ) from None
    model = CorrectionSubmission if event_type == "knowledge_correction" else GapSubmission
    try:
        submission: FeedbackSubmission = model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidSubmission(exc.errors()) from None
    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError:
        raise InvalidIdempotencyKey from None
    try:
        if submitted_via == "rest" and apply_ip_limit:
            limiter.check("ip", client_ip, settings.feedback_rate_limit_per_ip)
        if principal.token_id is not None:
            limiter.check("token", principal.token_id, settings.feedback_rate_limit_per_token)
    except HTTPException as exc:
        raise RateLimited(int((exc.headers or {}).get("Retry-After", "1"))) from None

    def persist() -> SubmissionResult:
        for attempt in range(3):
            try:
                with session_factory() as db:
                    return _submit(
                        db,
                        principal,
                        submission,
                        key,
                        window_hours=settings.idempotency_window_hours,
                        submitted_via=submitted_via,
                    )
            except OperationalError as exc:
                if not _sqlite_busy(exc):
                    raise
                if attempt == 2:
                    raise StorageBusy from exc
                time.sleep(0.1 * (attempt + 1))
        raise AssertionError("unreachable")

    return await run_in_threadpool(persist)


__all__ = [
    "IdempotencyConflict",
    "InvalidIdempotencyKey",
    "InvalidSubmission",
    "NotAuthorized",
    "RateLimited",
    "StorageBusy",
    "ingest",
]
