"""Local administrator authentication and opaque session handling."""

import hashlib
import hmac
import secrets
from datetime import UTC, timedelta
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from kebivo.models import AdminSession, AdminUser, AuditEvent, LoginAttempt, now_utc
from kebivo.settings import Settings

COOKIE_NAME = "kebivo_admin_session"
SESSION_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 30 * 60
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LIMIT = 5
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_DEVELOPMENT_KEY = secrets.token_bytes(32)
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerificationError, VerifyMismatchError):
        return False


def _key(settings: Settings) -> bytes:
    secret = settings.secret_key
    if not secret:
        # Development may use an ephemeral process key; production requires SECRET_KEY.
        return _DEVELOPMENT_KEY
    return secret.encode("utf-8")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_digest(settings: Any, value: str) -> str:
    return hmac.new(_key(settings), value.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_token(settings: Any, session_token: str) -> str:
    return hmac.new(_key(settings), b"csrf:" + session_token.encode(), hashlib.sha256).hexdigest()


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin and origin in request.app.state.settings.allowed_origins)


def wrong_origin_error(request: Request) -> HTTPException:
    configured = ", ".join(request.app.state.settings.allowed_origins) or "none"
    return HTTPException(
        status_code=403,
        detail=f"Page origin is not in ALLOWED_ORIGINS. Configured origins: {configured}",
    )


def _peer_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer not in request.app.state.settings.trusted_proxy_ips:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    # The configured proxy must replace X-Forwarded-For with the immediate client.
    candidate = forwarded.split(",", 1)[0].strip()
    try:
        import ipaddress

        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def _audit(db: Session, action: str, admin_id: str | None = None) -> None:
    db.add(
        AuditEvent(
            actor_type="admin" if admin_id else "system",
            actor_ref=admin_id,
            action=action,
            subject_type="admin_user",
            subject_id=admin_id,
            details_json="{}",
        )
    )


def _set_auth_cookie(response: Any, token: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def _auth_failure() -> HTTPException:
    return HTTPException(status_code=401, detail="Authentication required")


def require_admin(request: Request) -> tuple[AdminUser, AdminSession, str]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 200:
        raise _auth_failure()
    db: Session = request.state.db
    session_row = db.scalar(select(AdminSession).where(AdminSession.token_hash == _digest(token)))
    now = now_utc()
    if (
        session_row is None
        or session_row.expires_at.replace(tzinfo=UTC) <= now
        or session_row.revoked_at is not None
        or session_row.last_seen_at is None
        or session_row.last_seen_at.replace(tzinfo=UTC) < now - IDLE_SECONDS * timedelta(seconds=1)
    ):
        raise _auth_failure()
    admin = db.get(AdminUser, session_row.admin_user_id)
    if admin is None or not admin.is_active:
        raise _auth_failure()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not origin_allowed(request):
            raise wrong_origin_error(request)
        supplied_csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied_csrf, csrf_token(request.app.state.settings, token)):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
    # Simulation is read-only, including its authentication path.
    if not request.url.path.endswith("/routing/simulate"):
        session_row.last_seen_at = now
        db.commit()
    return admin, session_row, csrf_token(request.app.state.settings, token)


AdminAuth = Annotated[tuple[AdminUser, AdminSession, str], Depends(require_admin)]


def install_auth_routes(app: Any) -> None:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field

    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    class LoginBody(BaseModel):
        email: str = Field(min_length=3, max_length=320)
        password: str = Field(min_length=1, max_length=1024)

    @router.post("/login")
    def login(body: LoginBody, request: Request) -> JSONResponse:
        if not origin_allowed(request):
            raise wrong_origin_error(request)
        db: Session = request.state.db
        email = normalize_email(body.email)
        settings = request.app.state.settings
        identity_key = _private_digest(settings, "email:" + email)
        source_key = _private_digest(settings, "ip:" + _peer_ip(request))[:40]
        since = now_utc() - LOGIN_WINDOW
        failures = (
            db.scalar(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    or_(LoginAttempt.email == identity_key, LoginAttempt.ip_address == source_key),
                    LoginAttempt.succeeded.is_(False),
                    LoginAttempt.attempted_at >= since,
                )
            )
            or 0
        )
        if failures >= LOGIN_LIMIT:
            raise HTTPException(status_code=429, detail="Too many login attempts")

        attempt_count = db.scalar(select(func.count()).select_from(LoginAttempt)) or 0
        if attempt_count >= 20_000:
            oldest_ids = select(LoginAttempt.id).order_by(LoginAttempt.attempted_at).limit(1_000)
            db.execute(delete(LoginAttempt).where(LoginAttempt.id.in_(oldest_ids)))
        stale_ids = (
            select(LoginAttempt.id)
            .where(LoginAttempt.attempted_at < now_utc() - timedelta(days=30))
            .order_by(LoginAttempt.attempted_at)
            .limit(500)
        )
        db.execute(delete(LoginAttempt).where(LoginAttempt.id.in_(stale_ids)))

        admin = db.scalar(select(AdminUser).where(AdminUser.email == email))
        verified = verify_password(
            admin.password_hash if admin is not None else _DUMMY_PASSWORD_HASH, body.password
        )
        valid = bool(admin and admin.is_active and verified)
        db.add(
            LoginAttempt(
                email=identity_key,
                ip_address=source_key,
                succeeded=valid,
                attempted_at=now_utc(),
            )
        )
        if not valid:
            _audit(db, "admin.login_failed")
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid email or password")

        assert admin is not None
        now = now_utc()
        token = secrets.token_urlsafe(32)
        session_row = AdminSession(
            admin_user_id=admin.id,
            token_hash=_digest(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=SESSION_SECONDS),
        )
        admin.updated_at = now
        admin.last_login_at = now
        _audit(db, "admin.login_succeeded", admin.id)
        db.add(session_row)
        db.commit()
        response = JSONResponse(
            {
                "admin": {"id": admin.id, "email": admin.email},
                "csrf_token": csrf_token(settings, token),
            }
        )
        _set_auth_cookie(response, token, secure=request.headers["origin"].startswith("https://"))
        return response

    @router.get("/session")
    def get_session(request: Request, auth: AdminAuth) -> JSONResponse:
        admin, _, csrf = auth
        response = JSONResponse(
            {"admin": {"id": admin.id, "email": admin.email}, "csrf_token": csrf}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/logout")
    def logout(request: Request, auth: AdminAuth) -> JSONResponse:
        admin, session_row, _ = auth
        session_row.revoked_at = now_utc()
        _audit(request.state.db, "admin.logout", admin.id)
        request.state.db.commit()
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            httponly=True,
            secure=request.headers["origin"].startswith("https://"),
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)


def create_admin(db: Session, email: str, password: str, *, reset: bool = False) -> AdminUser:
    normalized = normalize_email(email)
    admin = db.scalar(select(AdminUser).where(AdminUser.email == normalized))
    if reset:
        if admin is None:
            raise ValueError("Administrator not found")
        admin.password_hash = hash_password(password)
        admin.updated_at = now_utc()
        db.execute(
            update(AdminSession)
            .where(AdminSession.admin_user_id == admin.id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=now_utc())
        )
        # Attempts are stored as keyed digests; clear them all so a throttle
        # built from repeated failures cannot keep the reset administrator out.
        db.execute(delete(LoginAttempt))
        _audit(db, "admin.password_reset", admin.id)
    else:
        if db.scalar(select(AdminUser.id).limit(1)) is not None:
            raise ValueError("An administrator already exists; use reset-password")
        admin = AdminUser(email=normalized, password_hash=hash_password(password))
        db.add(admin)
        db.flush()
        _audit(db, "admin.bootstrap", admin.id)
    db.commit()
    return admin
