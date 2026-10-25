"""ASGI application shell and foundation health endpoints."""

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kebivo.admin_feedback import install_admin_feedback_routes
from kebivo.admin_group_membership import install_admin_group_membership_routes
from kebivo.admin_group_mutations import install_admin_group_mutation_routes
from kebivo.admin_groups import install_admin_group_routes
from kebivo.admin_settings import install_admin_settings_routes
from kebivo.auth import install_auth_routes
from kebivo.db import make_engine
from kebivo.dedup.routes import install_dedup_routes
from kebivo.deliveries import install_delivery_routes, install_test_route
from kebivo.destinations.routes import install_destination_routes
from kebivo.errors import error_response, install_error_handlers
from kebivo.feedback.routes import install_feedback_routes
from kebivo.feedback_guard import FeedbackBodyGuard
from kebivo.feedback_limits import FixedWindowLimiter
from kebivo.llm.routes import install_llm_provider_routes
from kebivo.mcp_server import install_mcp_route
from kebivo.projects import install_project_routes
from kebivo.routing.routes import install_routing_routes
from kebivo.security.secretbox import SecretBox
from kebivo.settings import Settings
from kebivo.tokens import install_token_routes
from kebivo.worker.runner import Worker

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class JsonFormatter(logging.Formatter):
    """Format log records as compact JSON without serializing arbitrary messages."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        return json.dumps({"level": record.levelname.lower(), "event": record.msg, **fields})


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("kebivo.request")
    if not any(getattr(handler, "_kebivo_json_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._kebivo_json_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logger.propagate = False
    return logger


class RequestLoggingMiddleware:
    """Log request metadata only; headers and request bodies are never serialized."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        supplied_id = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied_id if _REQUEST_ID.fullmatch(supplied_id) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            (
                                b"default-src 'self'; object-src 'none'; "
                                b"base-uri 'self'; frame-ancestors 'none'"
                            ),
                        ),
                        (b"x-frame-options", b"DENY"),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _configure_logging().info(
                "http_request",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": scope.get("method", ""),
                        "path": scope.get("path", ""),
                        "status_code": status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                },
            )


def _probe_sqlite(path_value: str) -> None:
    """Open the configured SQLite database and run a harmless query."""
    if path_value != ":memory:":
        Path(path_value).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path_value, timeout=1)
    try:
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()


def _probe_fts5() -> bool:
    """Check the SQLite features required for securely deleted search terms."""
    if sqlite3.sqlite_version_info < (3, 42, 0):
        return False
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(value)")
            connection.execute("INSERT INTO fts_probe(fts_probe, rank) VALUES('secure-delete', 1)")
            connection.execute("DROP TABLE fts_probe")
    except sqlite3.Error:
        return False
    return True


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the API application using validated settings."""
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        app.state.search_ready = _probe_fts5()
        tables = set(inspect(app.state.engine).get_table_names())
        if app.state.worker is not None and not {"processing_jobs", "deliveries"} <= tables:
            app.state.worker = None
        if "destinations" in tables:
            box = SecretBox.from_settings(configured)
            with app.state.engine.connect() as connection:
                unknown = (
                    connection.execute(
                        text(
                            "SELECT id FROM destinations WHERE secrets_key_id IS NOT NULL "
                            "AND secrets_key_id NOT IN (:current, :previous) ORDER BY id"
                        ),
                        {"current": box.current_key_id, "previous": box.previous_key_id or ""},
                    )
                    .scalars()
                    .all()
                )
            if unknown:
                raise RuntimeError(
                    "Destination secrets use an unavailable key: " + ", ".join(unknown)
                )
        logging.getLogger("mcp").setLevel(logging.WARNING)
        # The SDK's security warning interpolates untrusted Host/Origin values.
        logging.getLogger("mcp.server.transport_security").setLevel(logging.ERROR)
        try:

            async def run_worker() -> AsyncIterator[None]:
                if app.state.worker is not None:
                    await app.state.worker.start()
                try:
                    yield
                finally:
                    if app.state.worker is not None:
                        await app.state.worker.stop()

            if mcp_server is None:
                async with asynccontextmanager(run_worker)():
                    yield
            else:
                manager = mcp_server._session_manager
                if manager is None:
                    raise RuntimeError("MCP session manager is unavailable")
                async with manager.run():
                    async with asynccontextmanager(run_worker)():
                        yield
        finally:
            logging.getLogger("kebivo.request").info("application_shutdown", extra={"fields": {}})
            app.state.engine.dispose()

    app = FastAPI(title=configured.display_name, lifespan=lifespan, openapi_url=None)
    app.state.database_path = configured.database_path
    app.state.search_ready = _probe_fts5()
    app.state.settings = configured
    app.state.feedback_limiter = FixedWindowLimiter()
    install_error_handlers(app)
    database_url = configured.database_url or (
        "sqlite://"
        if configured.database_path == ":memory:"
        else f"sqlite:///{configured.database_path}"
    )
    app.state.engine = make_engine(database_url)
    app.state.session_factory = sessionmaker(
        bind=app.state.engine, autoflush=False, expire_on_commit=False
    )
    app.state.llm_semaphore = asyncio.Semaphore(configured.llm_max_concurrency)
    app.state.worker = (
        Worker(app.state.session_factory, configured, app.state.llm_semaphore)
        if configured.worker_enabled
        else None
    )

    @app.middleware("http")
    async def database_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/mcp":
            return await call_next(request)
        request.state.db = app.state.session_factory()
        try:
            response = await call_next(request)
            if (
                request.url.path.startswith("/api/v1/auth/")
                or (
                    request.url.path.startswith("/api/v1/projects/")
                    and "/tokens" in request.url.path
                )
                or request.url.path == "/api/v1/feedback"
                or request.url.path.startswith("/api/v1/admin/feedback")
                or request.url.path.startswith("/api/v1/admin/groups")
                or request.url.path.startswith("/api/v1/admin/llm-providers")
                or request.url.path == "/api/v1/admin/settings"
                or request.url.path.startswith("/api/v1/admin/projects/")
                or request.url.path.startswith("/api/v1/destinations")
                or request.url.path.startswith("/api/v1/deliveries")
                or request.url.path.startswith("/api/v1/projects")
            ):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            request.state.db.close()

    app.add_middleware(FeedbackBodyGuard)
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        """Report that the process can serve requests."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], response_model=None)
    def ready() -> JSONResponse | dict[str, str]:
        """Report readiness only when SQLite can be opened and queried."""
        try:
            _probe_sqlite(app.state.database_path)
        except (OSError, sqlite3.Error):
            worker_state = "disabled" if app.state.worker is None else "stopped"
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "unavailable", "worker": worker_state},
            )
        if not app.state.search_ready:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "database": "ok",
                    "search": "unavailable",
                    "worker": "disabled" if app.state.worker is None else "stopped",
                },
            )
        if app.state.worker is None:
            worker_state = "disabled"
        elif app.state.worker.task is None or app.state.worker.task.done():
            worker_state = "stopped"
        else:
            worker_state = "ok"
        if worker_state == "stopped":
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "ok", "worker": worker_state},
            )
        return {"status": "ok", "database": "ok", "search": "ok", "worker": worker_state}

    # Register the implemented auth, project administration, and token APIs.
    install_auth_routes(app)
    install_project_routes(app)
    install_destination_routes(app)
    install_llm_provider_routes(app)
    install_test_route(app)
    install_delivery_routes(app)
    install_routing_routes(app)
    install_token_routes(app)
    install_feedback_routes(app)
    install_admin_feedback_routes(app)
    install_admin_group_routes(app)
    install_dedup_routes(app)
    install_admin_group_mutation_routes(app)
    install_admin_group_membership_routes(app)
    install_admin_settings_routes(app)
    mcp_server = install_mcp_route(app)

    # Serve the built Vite bundle when present (development keeps using Vite's server).
    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    index_file = frontend_dist / "index.html"
    if index_file.is_file():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        def frontend_root() -> FileResponse:
            return FileResponse(index_file)

        @app.get("/{frontend_path:path}", include_in_schema=False, response_model=None)
        def frontend_fallback(frontend_path: str, request: Request) -> FileResponse | JSONResponse:
            # Unknown API paths must remain API errors, never receive SPA HTML.
            if frontend_path == "api" or frontend_path.startswith("api/"):
                return error_response(
                    404, "not_found", "Not Found", getattr(request.state, "request_id", None)
                )
            if frontend_path == "mcp" or frontend_path.startswith("mcp/"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            if frontend_path.startswith(".well-known/oauth-"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            candidate = frontend_dist / frontend_path
            try:
                if candidate.is_file() and frontend_dist in candidate.resolve().parents:
                    return FileResponse(candidate)
            except (OSError, ValueError):
                # Over-long or malformed names are not files; serve the app shell.
                pass
            return FileResponse(index_file)

    return app


app = create_app()
