"""Authenticated project settings and lifecycle API."""

import base64
import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kebivo.admin_groups import _reject_bearer
from kebivo.auth import AdminAuth, _peer_ip
from kebivo.feedback.ingest import RateLimited, ingest
from kebivo.feedback.tags import normalize_tag_v1
from kebivo.models import (
    AdminUser,
    AuditEvent,
    Destination,
    EmbeddingBackfill,
    FeedbackGroupMember,
    LlmProviderConfig,
    Project,
    Tag,
    now_utc,
)
from kebivo.tokens import GAP_SCOPE, ProjectTokenPrincipal

SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
DEFAULT_SETTINGS = {"corrections": {"enabled": False}, "deduplication": {"mode": "exact"}}
EMBEDDING_SIMILARITY_DEFAULT = 0.93
EMBEDDING_MAX_SCAN_DEFAULT = 2000
EMBEDDING_MAX_SCAN_CAP = 5000
MAX_PAGE_SIZE = 100


class ProjectAPIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    settings: dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_SETTINGS),
        json_schema_extra={
            "examples": [
                {
                    "deduplication": {
                        "mode": "semantic",
                        "semantic": {
                            "method": "embeddings",
                            "provider_id": "00000000-0000-0000-0000-000000000000",
                            "embedding_model": "text-embedding-3-small",
                            "similarity_threshold": EMBEDDING_SIMILARITY_DEFAULT,
                            "max_scan": EMBEDDING_MAX_SCAN_DEFAULT,
                        },
                        "privacy_acknowledged": True,
                    }
                }
            ]
        },
    )

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _settings({"deduplication": {"mode": "exact"}, **value})

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG.fullmatch(value):
            raise ValueError("Slug must contain lowercase letters, numbers, and internal hyphens")
        return value


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    is_active: bool | None = None
    settings: dict[str, Any] | None = None
    fallback_destination_id: str | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Name cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Slug cannot be null")
        value = value.strip().lower()
        if not SLUG.fullmatch(value):
            raise ValueError("Slug must contain lowercase letters, numbers, and internal hyphens")
        return value

    @field_validator("is_active", "settings")
    @classmethod
    def non_nullable_settings(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _settings(value) if value is not None else None


def _settings(value: dict[str, Any]) -> dict[str, Any]:
    if "deduplication" not in value:
        return value
    dedup = value["deduplication"]
    if not isinstance(dedup, dict):
        raise ValueError("deduplication must be an object")
    if set(dedup) == {"enabled"} and isinstance(dedup["enabled"], bool):
        value = {**value, "deduplication": {"mode": "exact" if dedup["enabled"] else "disabled"}}
    elif dedup.get("mode") == "semantic":
        if (
            set(dedup) != {"mode", "semantic", "privacy_acknowledged"}
            or dedup["privacy_acknowledged"] is not True
        ):
            raise ValueError("semantic mode requires privacy acknowledgement")
        semantic = dedup["semantic"]
        if not isinstance(semantic, dict):
            raise ValueError("invalid semantic settings")
        method = semantic.get("method", "judge")
        allowed = {
            "provider_id",
            "timeout_seconds",
            "max_attempts",
            "method",
        } | (
            {"model", "candidate_count", "min_confidence"}
            if method == "judge"
            else {"embedding_model", "embedding_dimensions", "similarity_threshold", "max_scan"}
        )
        if method not in {"judge", "embeddings"} or set(semantic) - allowed:
            raise ValueError("invalid semantic settings")
        try:
            provider_id = str(UUID(semantic["provider_id"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("semantic provider_id must be a UUID") from None
        fields = {
            "timeout_seconds": (15, 1, 30),
            "max_attempts": (2, 1, 3),
        }
        if method == "judge":
            fields["candidate_count"] = (5, 1, 10)
        else:
            fields["max_scan"] = (EMBEDDING_MAX_SCAN_DEFAULT, 1, EMBEDDING_MAX_SCAN_CAP)
        normalized = {"provider_id": provider_id, "method": method}
        if method == "judge":
            model = semantic.get("model")
            if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 200):
                raise ValueError("invalid semantic model")
            normalized["model"] = model
        else:
            model = semantic.get("embedding_model")
            if not isinstance(model, str) or not 1 <= len(model) <= 200:
                raise ValueError("invalid semantic embedding_model")
            normalized["embedding_model"] = model
        for name, (default, low, high) in fields.items():
            number = semantic.get(name, default)
            if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
                raise ValueError(f"invalid semantic {name}")
            normalized[name] = number
        confidence_name = "min_confidence" if method == "judge" else "similarity_threshold"
        confidence = semantic.get(
            confidence_name, 0.85 if method == "judge" else EMBEDDING_SIMILARITY_DEFAULT
        )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0.5 <= confidence <= (1 if method == "judge" else 0.99)
        ):
            raise ValueError(f"invalid semantic {confidence_name}")
        normalized[confidence_name] = confidence
        value = {
            **value,
            "deduplication": {
                "mode": "semantic",
                "semantic": normalized,
                "privacy_acknowledged": True,
            },
        }
    elif set(dedup) != {"mode"} or dedup.get("mode") not in {"disabled", "exact"}:
        raise ValueError("deduplication mode must be disabled, exact or semantic")
    return value


def _require_provider(db: Session, settings: dict[str, Any]) -> None:
    dedup = settings.get("deduplication", {})
    if dedup.get("mode") != "semantic":
        return
    provider_id = dedup["semantic"]["provider_id"]
    provider = db.get(LlmProviderConfig, provider_id)
    if provider is None or not provider.enabled:
        raise ProjectAPIError(
            422, "invalid_provider", "Semantic provider must exist and be enabled"
        )
    if dedup["semantic"].get("method") == "embeddings" and provider.provider_type not in {
        "openai_compatible",
        "local",
    }:
        raise ProjectAPIError(422, "invalid_provider", "Provider does not support embeddings")


class DefaultTagsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[str]


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, project_id = value.split("|", 1)
        return datetime.fromisoformat(created_at), str(UUID(project_id))
    except (ValueError, UnicodeDecodeError):
        raise ProjectAPIError(400, "invalid_cursor", "Cursor is invalid") from None


def _encode_cursor(project: Project) -> str:
    value = f"{project.created_at.isoformat()}|{project.id}"
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _serialize(project: Project, db: Session) -> dict[str, Any]:
    tags = db.scalars(select(Tag).where(Tag.project_id == project.id).order_by(Tag.slug)).all()
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "is_active": project.is_active,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        "settings": json.loads(project.settings_json),
        "fallback_destination_id": project.fallback_destination_id,
        "tags": [
            {"id": tag.id, "name": tag.name, "slug": tag.slug, "is_default": tag.is_default}
            for tag in tags
        ],
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _get_project(db: Session, project_id: str) -> Project:
    try:
        canonical_id = str(UUID(project_id))
    except ValueError:
        raise ProjectAPIError(404, "project_not_found", "Project not found") from None
    project = db.get(Project, canonical_id)
    if project is None:
        raise ProjectAPIError(404, "project_not_found", "Project not found")
    return project


def _audit_project(
    db: Session,
    action: str,
    admin: AdminUser,
    project: Project,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin.id,
            action=action,
            subject_type="project",
            subject_id=project.id,
            details_json=json.dumps(details or {}, separators=(",", ":")),
        )
    )


def _reset_backfill(
    job: EmbeddingBackfill, semantic: dict[str, Any], embedding_revision: str
) -> None:
    job.provider_id = semantic["provider_id"]
    job.embedding_revision = embedding_revision
    job.model = semantic["embedding_model"]
    job.dims = None
    job.remaining = semantic["max_scan"]
    job.cursor_seen_at = None
    job.cursor_group_id = None


def install_project_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

    @router.get("/dedup/defaults", dependencies=[Depends(_reject_bearer)])
    def dedup_defaults(auth: AdminAuth) -> dict[str, Any]:
        del auth
        return {
            "embeddings": {
                "similarity_threshold": EMBEDDING_SIMILARITY_DEFAULT,
                "max_scan": EMBEDDING_MAX_SCAN_DEFAULT,
                "max_scan_cap": EMBEDDING_MAX_SCAN_CAP,
            }
        }

    @router.post(
        "/{project_id}/test-event", status_code=202, dependencies=[Depends(_reject_bearer)]
    )
    async def send_test_event(project_id: str, request: Request, auth: AdminAuth) -> dict[str, str]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _get_project(db, project_id)
        if project.archived_at is not None or not project.is_active:
            raise ProjectAPIError(
                409, "project_inactive", "Project must be active to send a test event"
            )
        request.app.state.feedback_limiter.check("admin_test_event", admin.id, 10)
        try:
            result = await ingest(
                request.app.state.session_factory,
                request.app.state.settings,
                request.app.state.feedback_limiter,
                ProjectTokenPrincipal(project.id, None, frozenset({GAP_SCOPE})),
                {
                    "event_type": "knowledge_gap",
                    "question": "Kebivo test event: How do I reset my corporate VPN?",
                    "tags": ["kebivo-test"],
                    "agent_id": "kebivo-test",
                },
                None,
                submitted_via="rest",
                client_ip=_peer_ip(request),
                apply_ip_limit=False,
            )
        except RateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail="Feedback rate limit exceeded",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        group_id = db.scalar(
            select(FeedbackGroupMember.group_id).where(
                FeedbackGroupMember.event_id == result.event_id
            )
        )
        assert group_id is not None
        _audit_project(db, "project.test_event_sent", admin, project, {"event_id": result.event_id})
        db.commit()
        if request.app.state.worker is not None:
            request.app.state.worker.wake()
        return {"event_id": result.event_id, "group_id": group_id}

    @router.post("", status_code=201)
    def create_project(body: ProjectCreate, request: Request, auth: AdminAuth) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        _require_provider(db, body.settings)
        project = Project(
            name=body.name,
            slug=body.slug,
            description=body.description,
            settings_json=json.dumps(body.settings, separators=(",", ":")),
            created_by_admin_id=admin.id,
        )
        db.add(project)
        try:
            db.flush()
            _audit_project(
                db,
                "project.created",
                admin,
                project,
                {
                    "semantic_privacy_acknowledged": body.settings.get("deduplication", {}).get(
                        "privacy_acknowledged"
                    )
                    is True
                },
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ProjectAPIError(
                409, "slug_conflict", "A project with this slug already exists"
            ) from None
        return _serialize(project, db)

    @router.get("")
    def list_projects(
        request: Request,
        auth: AdminAuth,
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        del auth
        db: Session = request.state.db
        statement = select(Project)
        if not include_archived:
            statement = statement.where(Project.archived_at.is_(None))
        position = _decode_cursor(cursor)
        if position:
            created_at, project_id = position
            statement = statement.where(
                or_(
                    Project.created_at > created_at,
                    and_(Project.created_at == created_at, Project.id > project_id),
                )
            )
        rows = list(
            db.scalars(statement.order_by(Project.created_at, Project.id).limit(limit + 1)).all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [_serialize(project, db) for project in rows],
            "next_cursor": _encode_cursor(rows[-1]) if has_more and rows else None,
        }

    @router.get("/{project_id}")
    def get_project(project_id: str, request: Request, auth: AdminAuth) -> dict[str, Any]:
        del auth
        return _serialize(_get_project(request.state.db, project_id), request.state.db)

    @router.put("/{project_id}/default-tags")
    def put_default_tags(
        project_id: str,
        body: DefaultTagsUpdate,
        request: Request,
        response: Response,
        auth: AdminAuth,
    ) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _get_project(db, project_id)
        if project.archived_at is not None:
            raise ProjectAPIError(409, "project_archived", "Archived projects cannot be edited")
        wanted: dict[str, str] = {}
        for index, name in enumerate(body.tags):
            try:
                slug = normalize_tag_v1(name)
            except ValueError as exc:
                raise ProjectAPIError(422, "invalid_tag", f"tags[{index}]: {exc}") from None
            wanted.setdefault(slug, name.strip())
        existing = {
            tag.slug: tag for tag in db.scalars(select(Tag).where(Tag.project_id == project.id))
        }
        for tag in existing.values():
            tag.is_default = tag.slug in wanted
        for slug, name in wanted.items():
            if slug not in existing:
                db.add(Tag(project_id=project.id, slug=slug, name=name, is_default=True))
        project.updated_at = now_utc()
        _audit_project(db, "project.default_tags_updated", admin, project, {"slugs": list(wanted)})
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        return _serialize(project, db)

    @router.patch("/{project_id}")
    def update_project(
        project_id: str, body: ProjectUpdate, request: Request, auth: AdminAuth
    ) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _get_project(db, project_id)
        old_dedup = json.loads(project.settings_json).get("deduplication", {})
        changes = body.model_dump(exclude_unset=True)
        if "fallback_destination_id" in changes and changes["fallback_destination_id"] is not None:
            try:
                changes["fallback_destination_id"] = str(UUID(changes["fallback_destination_id"]))
            except ValueError:
                raise ProjectAPIError(
                    422, "invalid_destination", "Fallback destination is invalid"
                ) from None
            if db.get(Destination, changes["fallback_destination_id"]) is None:
                raise ProjectAPIError(
                    422, "invalid_destination", "Fallback destination does not exist"
                )
        if "settings" in changes and changes["settings"] is not None:
            _require_provider(db, changes["settings"])
            acknowledged = (
                changes["settings"].get("deduplication", {}).get("privacy_acknowledged") is True
            )
            project.settings_json = json.dumps(
                {**json.loads(project.settings_json), **changes.pop("settings")},
                separators=(",", ":"),
            )
            new_dedup = json.loads(project.settings_json).get("deduplication", {})
            new_semantic = new_dedup.get("semantic", {})
            old_semantic = old_dedup.get("semantic", {})
            embedding_enabled = (
                new_dedup.get("mode") == "semantic" and new_semantic.get("method") == "embeddings"
            )
            changed = any(
                new_semantic.get(key) != old_semantic.get(key)
                for key in ("provider_id", "embedding_model")
            )
            if embedding_enabled and (
                old_dedup.get("mode") != "semantic"
                or old_semantic.get("method") != "embeddings"
                or changed
            ):
                backfill = db.get(EmbeddingBackfill, project.id)
                if backfill is None:
                    backfill = EmbeddingBackfill(project_id=project.id)
                    db.add(backfill)
                provider = db.get(LlmProviderConfig, new_semantic["provider_id"])
                assert provider is not None
                _reset_backfill(backfill, new_semantic, provider.embedding_revision)
            elif not embedding_enabled:
                backfill = db.get(EmbeddingBackfill, project.id)
                if backfill is not None:
                    db.delete(backfill)
        else:
            acknowledged = False
        for key, value in changes.items():
            setattr(project, key, value)
        project.updated_at = now_utc()
        try:
            _audit_project(
                db,
                "project.updated",
                admin,
                project,
                {"semantic_privacy_acknowledged": acknowledged},
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ProjectAPIError(
                409, "slug_conflict", "A project with this slug already exists"
            ) from None
        return _serialize(project, db)

    @router.post("/{project_id}/archive")
    def archive_project(project_id: str, request: Request, auth: AdminAuth) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _get_project(db, project_id)
        if project.archived_at is None:
            project.archived_at = now_utc()
            project.is_active = False
            project.updated_at = now_utc()
            _audit_project(db, "project.archived", admin, project)
            db.commit()
        return _serialize(project, db)

    @router.post("/{project_id}/restore")
    def restore_project(project_id: str, request: Request, auth: AdminAuth) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = _get_project(db, project_id)
        if project.archived_at is not None:
            project.archived_at = None
            project.updated_at = now_utc()
            _audit_project(db, "project.restored", admin, project)
            db.commit()
        return _serialize(project, db)

    async def project_error_handler(_request: Request, exc: ProjectAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.add_exception_handler(ProjectAPIError, project_error_handler)
    app.include_router(router)
