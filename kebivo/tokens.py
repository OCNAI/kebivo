"""Project API tokens and reusable project-scoped bearer verification."""

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.models import ApiToken, AuditEvent, Project, now_utc

GAP_SCOPE = "feedback:gap"
CORRECTION_SCOPE = "feedback:correction"
VALID_SCOPES = {GAP_SCOPE, CORRECTION_SCOPE}


@dataclass(frozen=True)
class ProjectTokenPrincipal:
    """Authenticated project identity and granted scopes, safe to pass to handlers."""

    project_id: str
    token_id: str | None
    scopes: frozenset[str]


def _token_digest(settings: Any, token: str) -> str:
    key = settings.secret_key.encode("utf-8") if settings.secret_key else b""
    if not key:
        # Config validation forbids this in production; don't make credentials
        # unusable in ephemeral test/development configurations.
        from kebivo.auth import _DEVELOPMENT_KEY

        key = _DEVELOPMENT_KEY
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def _corrections_enabled(project: Project) -> bool:
    try:
        settings = json.loads(project.settings_json)
    except (TypeError, json.JSONDecodeError):
        return False
    corrections = settings.get("corrections")
    return isinstance(corrections, dict) and corrections.get("enabled") is True


def authenticate_project_token(
    request: Request, bearer_token: str | None = None
) -> tuple[ProjectTokenPrincipal, Project]:
    """HTTP adapter for request-free bearer authentication."""
    if bearer_token is None:
        authorization = request.headers.get("authorization", "")
        scheme, separator, bearer_token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not bearer_token:
            raise HTTPException(status_code=401, detail="Authentication required")
    return authenticate_bearer(request.state.db, request.app.state.settings, bearer_token)


def authenticate_bearer(
    db: Session, settings: Any, token: str
) -> tuple[ProjectTokenPrincipal, Project]:
    """Authenticate a bearer credential and its active project; never commit."""
    if len(token) > 256 or "." not in token:
        raise HTTPException(status_code=401, detail="Invalid project token")
    prefix, _secret = token.split(".", 1)
    if not prefix.startswith("kebivo_pt_") or len(prefix) > 32:
        raise HTTPException(status_code=401, detail="Invalid project token")

    row = db.scalar(select(ApiToken).where(ApiToken.token_prefix == prefix))
    expected_digest = _token_digest(settings, token)
    if row is None or not hmac.compare_digest(row.token_hash, expected_digest):
        raise HTTPException(status_code=401, detail="Invalid project token")
    now = now_utc()
    expiry = row.expires_at
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if row.revoked_at is not None or (expiry is not None and expiry <= now):
        raise HTTPException(status_code=401, detail="Invalid project token")
    project = db.get(Project, row.project_id)
    if project is None or not project.is_active or project.archived_at is not None:
        raise HTTPException(status_code=401, detail="Invalid project token")
    try:
        scopes = frozenset(json.loads(row.scopes_json))
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid project token") from None
    return ProjectTokenPrincipal(project_id=project.id, token_id=row.id, scopes=scopes), project


def authorize_feedback(principal: ProjectTokenPrincipal, project: Project, event_type: str) -> None:
    """Apply scope and project correction policy to each submission."""
    scope = CORRECTION_SCOPE if event_type == "knowledge_correction" else GAP_SCOPE
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail="Project token scope required")
    if scope == CORRECTION_SCOPE and not _corrections_enabled(project):
        raise HTTPException(status_code=403, detail="Project corrections are disabled")


def touch_project_token(db: Session, token_id: str | None, *, now: datetime | None = None) -> None:
    """Stage the last-use update in the caller's transaction at most once per minute."""
    if token_id is None:
        return
    row = db.get(ApiToken, token_id)
    if row is None:
        return
    timestamp = now or now_utc()
    previous = row.last_used_at
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if previous is None or timestamp - previous >= timedelta(seconds=60):
        row.last_used_at = timestamp


def verify_project_token(
    request: Request, bearer_token: str, *, required_scope: str = GAP_SCOPE
) -> ProjectTokenPrincipal:
    """Compatibility wrapper for the Phase 1 scope verifier."""
    if required_scope not in VALID_SCOPES:
        raise ValueError(f"Unknown project token scope: {required_scope}")
    principal, project = authenticate_project_token(request, bearer_token)
    authorize_feedback(
        principal,
        project,
        "knowledge_correction" if required_scope == CORRECTION_SCOPE else "knowledge_gap",
    )
    return principal


def require_project_token(
    required_scope: str = GAP_SCOPE,
) -> Callable[[Request], ProjectTokenPrincipal]:
    """Build a FastAPI dependency for a downstream route's required scope."""
    if required_scope not in VALID_SCOPES:
        raise ValueError(f"Unknown project token scope: {required_scope}")

    def dependency(request: Request) -> ProjectTokenPrincipal:
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        return verify_project_token(request, token, required_scope=required_scope)

    return dependency


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: [GAP_SCOPE], min_length=1, max_length=2)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        if (
            len(set(value)) != len(value)
            or not set(value) <= VALID_SCOPES
            or GAP_SCOPE not in value
        ):
            raise ValueError("Scopes must include feedback:gap and contain only supported scopes")
        return sorted(value)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value <= datetime.now(UTC):
                raise ValueError("Expiry must be a future UTC timestamp")
            return value.astimezone(UTC)
        return value


def _token_metadata(row: ApiToken) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.token_prefix,
        "scopes": json.loads(row.scopes_json),
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def mint_project_token(
    db: Session,
    settings: Any,
    project: Project,
    admin_id: str,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> tuple[ApiToken, str]:
    prefix = "kebivo_pt_" + secrets.token_urlsafe(12).rstrip("=")
    token = prefix + "." + secrets.token_urlsafe(32)
    row = ApiToken(
        project_id=project.id,
        name=name,
        token_hash=_token_digest(settings, token),
        token_prefix=prefix,
        scopes_json=json.dumps(scopes, separators=(",", ":")),
        created_by_admin_id=admin_id,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin_id,
            action="project_token.created",
            subject_type="api_token",
            subject_id=row.id,
            details_json=json.dumps(
                {"project_id": project.id, "scopes": scopes}, separators=(",", ":")
            ),
        )
    )
    return row, token


def install_token_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/tokens", tags=["project tokens"])

    def project_or_404(db: Session, project_id: str) -> Project:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @router.post("", status_code=201)
    def create_token(
        project_id: str, body: TokenCreate, request: Request, auth: AdminAuth
    ) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project = project_or_404(db, project_id)
        row, token = mint_project_token(
            db, request.app.state.settings, project, admin.id,
            name=body.name, scopes=body.scopes, expires_at=body.expires_at,
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=503, detail="Unable to issue token; retry") from None
        return {**_token_metadata(row), "token": token}

    @router.get("")
    def list_tokens(project_id: str, request: Request, auth: AdminAuth) -> dict[str, Any]:
        del auth
        db: Session = request.state.db
        project_or_404(db, project_id)
        rows = db.scalars(
            select(ApiToken)
            .where(ApiToken.project_id == project_id)
            .order_by(ApiToken.created_at.desc(), ApiToken.id)
        ).all()
        return {"items": [_token_metadata(row) for row in rows]}

    @router.post("/{token_id}/revoke")
    def revoke_token(
        project_id: str, token_id: str, request: Request, auth: AdminAuth
    ) -> dict[str, Any]:
        admin, _, _ = auth
        db: Session = request.state.db
        project_or_404(db, project_id)
        row = db.scalar(
            select(ApiToken).where(ApiToken.id == token_id, ApiToken.project_id == project_id)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if row.revoked_at is None:
            row.revoked_at = now_utc()
            db.add(
                AuditEvent(
                    actor_type="admin",
                    actor_ref=admin.id,
                    action="project_token.revoked",
                    subject_type="api_token",
                    subject_id=row.id,
                    details_json=json.dumps({"project_id": project_id}, separators=(",", ":")),
                )
            )
            db.commit()
        return _token_metadata(row)

    app.include_router(router)
