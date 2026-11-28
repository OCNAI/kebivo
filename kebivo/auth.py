"""Local administrator authentication and opaque session handling."""

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from kebivo.models import AdminSession, AdminUser, AuditEvent, LoginAttempt, now_utc
from kebivo.settings import Settings

COOKIE_NAME = "kebivo_admin_session"
SESSION_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 30 * 60
LOGIN_WINDOW = timedelta(minutes=15)
# Hard lock: this many failures for one email from one source address.
LOGIN_LIMIT = 5
# One source address trying many emails is capped more loosely.
LOGIN_SOURCE_LIMIT = 20
# Failures for one email from all sources only add a capped, escalating delay.
# One source alone never reaches it, and a source that signed in to that email
# within LOGIN_KNOWN_SOURCE skips it, so attackers with many addresses can't
# hold the administrator out of their usual address.
LOGIN_EMAIL_FREE = 2 * LOGIN_LIMIT
LOGIN_EMAIL_MAX_DELAY = 60
LOGIN_KNOWN_SOURCE = timedelta(days=30)
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
    if not origin:
        return False
    configured = request.app.state.settings.allowed_origins
    if configured:
        return origin in configured
    try:
        parsed = urlsplit(origin)
        return bool(
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.netloc == request.headers.get("host")
            and not (
                parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password
            )
            and parsed.port != 0
        )
    except ValueError:
        return False


REMOTE_ACCESS_HINT = (
    "To sign in from another address, put an HTTPS reverse proxy in front of Kebivo "
    "and add its https:// origin to ALLOWED_ORIGINS, or open an SSH or VPN tunnel and "
    "use http://127.0.0.1. See https://docs.kebivo.com/self-hosting/#reverse-proxy-and-tls"
)


def wrong_origin_error(request: Request) -> HTTPException:
    configured = request.app.state.settings.allowed_origins
    return HTTPException(
        status_code=403,
        detail=(
            f"Page origin is not in ALLOWED_ORIGINS. Configured origins: {', '.join(configured)}"
            if configured
            else "Without ALLOWED_ORIGINS, Kebivo accepts the admin console only from localhost. "
            + REMOTE_ACCESS_HINT
        ),
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


def _login_wait(db: Session, identity_key: str, source_key: str) -> int:
    """Seconds this email and source must wait before another login attempt."""
    now = now_utc()
    failed = select(LoginAttempt.attempted_at).where(
        LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at >= now - LOGIN_WINDOW
    )

    def recent(*conditions: Any) -> list[datetime]:
        rows = db.scalars(failed.where(*conditions).order_by(LoginAttempt.attempted_at)).all()
        return [row if row.tzinfo else row.replace(tzinfo=UTC) for row in rows]

    def window_wait(times: list[datetime], limit: int) -> float:
        # The window reopens once enough of the oldest failures have aged out.
        if len(times) < limit:
            return 0
        return (times[len(times) - limit] + LOGIN_WINDOW - now).total_seconds()

    waits = [
        window_wait(
            recent(LoginAttempt.email == identity_key, LoginAttempt.ip_address == source_key),
            LOGIN_LIMIT,
        ),
        window_wait(recent(LoginAttempt.ip_address == source_key), LOGIN_SOURCE_LIMIT),
    ]
    by_email = recent(LoginAttempt.email == identity_key)
    known_source = (
        len(by_email) >= LOGIN_EMAIL_FREE
        and db.scalar(
            select(LoginAttempt.id)
            .where(
                LoginAttempt.succeeded.is_(True),
                LoginAttempt.email == identity_key,
                LoginAttempt.ip_address == source_key,
                LoginAttempt.attempted_at >= now - LOGIN_KNOWN_SOURCE,
            )
            .limit(1)
        )
        is not None
    )
    if len(by_email) >= LOGIN_EMAIL_FREE and not known_source:
        delay = min(2 ** min(len(by_email) - LOGIN_EMAIL_FREE, 6), LOGIN_EMAIL_MAX_DELAY)
        waits.append((by_email[-1] + timedelta(seconds=delay) - now).total_seconds())
    longest = max(waits)
    return int(longest) + 1 if longest > 0 else 0


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
    # These paths must not write during authentication.
    if not request.url.path.endswith(("/routing/simulate", "/test-event")):
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

    class SetupBody(BaseModel):
        token: str = Field(min_length=1, max_length=200)
        email: str = Field(min_length=3, max_length=320)
        password: str = Field(min_length=12, max_length=1024)

    def issue_session(db: Session, admin: AdminUser, request: Request) -> JSONResponse:
        now = now_utc()
        token = secrets.token_urlsafe(32)
        db.add(
            AdminSession(
                admin_user_id=admin.id,
                token_hash=_digest(token),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(seconds=SESSION_SECONDS),
            )
        )
        admin.updated_at = now
        admin.last_login_at = now
        _audit(db, "admin.login_succeeded", admin.id)
        db.commit()
        response = JSONResponse(
            {
                "admin": {"id": admin.id, "email": admin.email},
                "csrf_token": csrf_token(request.app.state.settings, token),
            }
        )
        _set_auth_cookie(response, token, secure=request.headers["origin"].startswith("https://"))
        return response

    @router.get("/setup")
    def setup_required(request: Request) -> dict[str, bool]:
        db: Session = request.state.db
        return {"required": db.scalar(select(AdminUser.id).limit(1)) is None}

    @router.post("/setup")
    def setup(body: SetupBody, request: Request) -> JSONResponse:
        if not origin_allowed(request):
            raise wrong_origin_error(request)
        token = getattr(request.app.state, "setup_token", None)
        deadline = getattr(request.app.state, "setup_deadline", 0.0)
        if (
            not token
            or time.monotonic() >= deadline
            or not hmac.compare_digest(body.token.encode("utf-8"), token.encode("utf-8"))
        ):
            raise HTTPException(status_code=403, detail="Invalid or expired setup link")
        email = normalize_email(body.email)
        if len(email) < 3:
            raise HTTPException(status_code=422, detail="Email must contain at least 3 characters")
        try:
            password_hash = hash_password(body.password)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Password must contain at least 12 characters"
            ) from None
        db: Session = request.state.db
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if db.scalar(select(AdminUser.id).limit(1)) is not None:
            db.rollback()
            raise HTTPException(status_code=409, detail="Administrator already exists")
        admin = AdminUser(email=email, password_hash=password_hash)
        db.add(admin)
        db.flush()
        _audit(db, "admin.bootstrap", admin.id)
        response = issue_session(db, admin, request)
        request.app.state.setup_token = None
        return response

    @router.post("/login")
    def login(body: LoginBody, request: Request) -> JSONResponse:
        if not origin_allowed(request):
            raise wrong_origin_error(request)
        db: Session = request.state.db
        email = normalize_email(body.email)
        settings = request.app.state.settings
        identity_key = _private_digest(settings, "email:" + email)
        source_key = _private_digest(settings, "ip:" + _peer_ip(request))[:40]

        def throttled(wait: int) -> HTTPException:
            return HTTPException(
                status_code=429,
                detail=f"Too many login attempts. Try again in {wait} seconds.",
                headers={"Retry-After": str(wait)},
            )

        # Reject throttled requests with a plain read, so a flood never takes the
        # write lock. Then check again and record the attempt in one write
        # transaction, before the slow password check, so parallel requests
        # cannot all pass the throttle.
        wait = _login_wait(db, identity_key, source_key)
        db.rollback()
        if wait:
            raise throttled(wait)
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        wait = _login_wait(db, identity_key, source_key)
        if wait:
            db.rollback()
            raise throttled(wait)

        attempt_count = db.scalar(select(func.count()).select_from(LoginAttempt)) or 0
        if attempt_count >= 20_000:
            # Keep sign-ins: they mark known sources and need the right password.
            oldest_ids = (
                select(LoginAttempt.id)
                .where(LoginAttempt.succeeded.is_(False))
                .order_by(LoginAttempt.attempted_at)
                .limit(1_000)
            )
            db.execute(delete(LoginAttempt).where(LoginAttempt.id.in_(oldest_ids)))
        stale_ids = (
            select(LoginAttempt.id)
            .where(LoginAttempt.attempted_at < now_utc() - timedelta(days=30))
            .order_by(LoginAttempt.attempted_at)
            .limit(500)
        )
        db.execute(delete(LoginAttempt).where(LoginAttempt.id.in_(stale_ids)))
        attempt = LoginAttempt(
            email=identity_key, ip_address=source_key, succeeded=False, attempted_at=now_utc()
        )
        db.add(attempt)
        db.commit()

        admin = db.scalar(select(AdminUser).where(AdminUser.email == email))
        verified = verify_password(
            admin.password_hash if admin is not None else _DUMMY_PASSWORD_HASH, body.password
        )
        if not (admin and admin.is_active and verified):
            _audit(db, "admin.login_failed")
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid email or password")

        attempt.succeeded = True
        assert admin is not None
        return issue_session(db, admin, request)

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
