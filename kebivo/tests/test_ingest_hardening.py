"""Shared ingestion and gate-review regressions."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from kebivo.db import make_engine
from kebivo.errors import install_error_handlers
from kebivo.feedback.ingest import (
    IdempotencyConflict,
    InvalidIdempotencyKey,
    InvalidSubmission,
    NotAuthorized,
    RateLimited,
    ingest,
)
from kebivo.feedback_limits import FixedWindowLimiter
from kebivo.models import FeedbackEvent, FeedbackEventTag, ProcessingJob, Tag, now_utc
from kebivo.tests.test_feedback_submission import counts, post, setup
from kebivo.tokens import ProjectTokenPrincipal, authenticate_bearer


@pytest.mark.parametrize(
    "field,value",
    [
        ("question", "a\ud800b"),
        ("correction", "a\ud800b"),
        ("agent_id", "a\ud800b"),
        ("source_event_id", "a\ud800b"),
        ("tags", ["a\ud800b"]),
        ("question", "a\x7fb"),
        ("conversation_id", "a\x85b"),
        ("tags", ["a\x85b"]),
        ("tags", ["A" + "_" * 70]),
        ("metadata", {"x": "a\ud800b"}),
    ],
)
def test_bad_text_rejected_without_rows(tmp_path: Path, field: str, value: Any) -> None:
    client, app, _, token = setup(tmp_path)
    payload = {"event_type": "knowledge_correction", "question": "Question", "correction": "Answer"}
    payload[field] = value
    before = counts(app)
    response = client.post(
        "/api/v1/feedback",
        content=json.dumps(payload, ensure_ascii=True).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert counts(app) == before


def test_tag_display_is_collapsed_and_single_line(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    response = post(
        client,
        token,
        {"event_type": "knowledge_gap", "question": "Q", "tags": ["  Customer \n  Service  "]},
    )
    assert response.status_code == 202
    with app.state.session_factory() as db:
        tag = db.scalar(select(Tag))
        assert tag is not None
        assert (tag.name, tag.slug) == ("Customer Service", "customer-service")


def test_ingest_domain_errors_and_channel(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    with app.state.session_factory() as db:
        principal, _ = authenticate_bearer(db, app.state.settings, token)
    payload = {"event_type": "knowledge_gap", "question": "Q"}
    limiter = FixedWindowLimiter()

    def submit(
        body: dict[str, Any], key: str | None = None, *, identity: ProjectTokenPrincipal = principal
    ) -> Any:
        return anyio.run(
            lambda: ingest(
                app.state.session_factory,
                app.state.settings,
                limiter,
                identity,
                body,
                key,
                submitted_via="mcp",
                client_ip="127.0.0.1",
            )
        )

    before = counts(app)
    with pytest.raises(NotAuthorized) as denied:
        submit(
            payload, identity=ProjectTokenPrincipal(project["id"], principal.token_id, frozenset())
        )
    assert denied.value.code == "scope_required"
    with pytest.raises(InvalidSubmission):
        submit({**payload, "question": ""})
    with pytest.raises(InvalidIdempotencyKey):
        submit(payload, "has space")
    assert counts(app) == before
    result = submit(payload, "channel")
    with app.state.session_factory() as db:
        row = db.get(FeedbackEvent, result.event_id)
        assert row is not None and row.submitted_via == "mcp"
    assert counts(app)[:4] == (1, 0, 1, 1)
    assert submit(payload, "channel").replayed
    before_conflict = counts(app)
    with pytest.raises(IdempotencyConflict):
        submit({**payload, "question": "Changed"}, "channel")
    assert counts(app) == before_conflict


def test_sqlite_busy_retry_replays_once_and_exhaustion_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, _, token = setup(tmp_path)
    from kebivo.feedback.service import submit_feedback as actual_submit

    attempts = 0

    def busy_twice(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OperationalError(
                "BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked")
            )
        return actual_submit(*args, **kwargs)

    monkeypatch.setattr("kebivo.feedback.routes.submit_feedback", busy_twice)
    monkeypatch.setattr("kebivo.feedback.ingest.time.sleep", lambda _: None)
    payload = {"event_type": "knowledge_gap", "question": "Busy test"}
    first = post(client, token, payload, "busy-once")
    assert first.status_code == 202 and attempts == 3
    replay = post(client, token, payload, "busy-once")
    assert replay.status_code == 202
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["event_id"] == first.json()["event_id"]
    assert counts(app)[:4] == (1, 0, 1, 1)

    def always_busy(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError(
            "BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked")
        )

    monkeypatch.setattr("kebivo.feedback.routes.submit_feedback", always_busy)
    before = counts(app)
    exhausted = post(client, token, payload, "busy-exhausted")
    assert exhausted.status_code == 503
    assert exhausted.headers["retry-after"] == "1"
    assert exhausted.json()["error"]["code"] == "storage_busy"
    assert counts(app) == before


def test_readiness_backlog_tracks_pending_and_drain(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    app.state.worker = None
    event_id = post(client, token, {"event_type": "knowledge_gap", "question": "Queued"}).json()[
        "event_id"
    ]
    with app.state.session_factory() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        assert job is not None
        from datetime import timedelta

        job.created_at = now_utc() - timedelta(seconds=20)
        db.commit()
    queued = client.get("/health/ready")
    assert queued.status_code == 200
    assert queued.json()["routing_backlog"]["pending_count"] == 1
    assert queued.json()["routing_backlog"]["oldest_pending_age_seconds"] >= 19
    with app.state.session_factory() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        assert job is not None
        job.status = "completed"
        db.commit()
    assert client.get("/health/ready").json()["routing_backlog"] == {
        "pending_count": 0,
        "oldest_pending_age_seconds": None,
    }


def test_rest_and_mcp_store_same_feedback_contract(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    payload = {
        "event_type": "knowledge_gap",
        "question": "Same question",
        "context": "Same context",
        "agent_id": "same-agent",
        "tags": ["Team  Support"],
        "metadata": {"source": "test"},
    }
    rest = post(client, token, payload)
    assert rest.status_code == 202
    with app.state.session_factory() as db:
        principal, _ = authenticate_bearer(db, app.state.settings, token)
    mcp = anyio.run(
        lambda: ingest(
            app.state.session_factory,
            app.state.settings,
            app.state.feedback_limiter,
            principal,
            payload,
            None,
            submitted_via="mcp",
            client_ip="127.0.0.1",
        )
    )
    with app.state.session_factory() as db:
        rest_row = db.get(FeedbackEvent, rest.json()["event_id"])
        mcp_row = db.get(FeedbackEvent, mcp.event_id)
        assert rest_row is not None and mcp_row is not None
        for field in (
            "project_id",
            "api_token_id",
            "event_type",
            "question",
            "context",
            "agent_id",
            "metadata_json",
            "normalized_question_hash",
            "processing_status",
        ):
            assert getattr(rest_row, field) == getattr(mcp_row, field)
        assert (rest_row.submitted_via, mcp_row.submitted_via) == ("rest", "mcp")
        links = db.execute(
            select(FeedbackEventTag.event_id, Tag.slug, FeedbackEventTag.from_submission).join(
                Tag, FeedbackEventTag.tag_id == Tag.id
            )
        ).all()
        assert {(slug, submitted) for _, slug, submitted in links} == {("team-support", True)}
        assert {event_id for event_id, _, _ in links} == {rest_row.id, mcp_row.id}
    assert counts(app) == (2, 2, 0, 2, 1)


def test_ingest_token_limit_has_no_extra_rows(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    with app.state.session_factory() as db:
        principal, _ = authenticate_bearer(db, app.state.settings, token)
    settings = replace(app.state.settings, feedback_rate_limit_per_token=1)
    limiter = FixedWindowLimiter()

    def submit() -> Any:
        return anyio.run(
            lambda: ingest(
                app.state.session_factory,
                settings,
                limiter,
                principal,
                {"event_type": "knowledge_gap", "question": "Q"},
                None,
                submitted_via="mcp",
                client_ip="127.0.0.1",
            )
        )

    submit()
    before = counts(app)
    with pytest.raises(RateLimited) as limited:
        submit()
    assert limited.value.retry_after >= 1
    assert counts(app) == before


def test_limiter_prunes_expired_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kebivo.feedback_limits.time.monotonic", lambda: 1.0)
    limiter = FixedWindowLimiter()
    for index in range(100):
        limiter.check("token", str(index), 1)
    assert len(limiter._counts) == 100
    monkeypatch.setattr("kebivo.feedback_limits.time.monotonic", lambda: 61.0)
    limiter.check("token", "new", 1)
    assert list(limiter._counts) == [("token", "new")]


def test_engine_hides_parameters_and_500_log_is_class_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'hidden.db'}")
    assert engine.hide_parameters is True
    engine.dispose()
    app = FastAPI()
    install_error_handlers(app)
    logged: list[tuple[Any, ...]] = []
    monkeypatch.setattr("kebivo.errors.logger.error", lambda *args: logged.append(args))

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("private question secret-key")

    response = TestClient(app, raise_server_exceptions=False).get("/explode")
    assert response.status_code == 500
    assert logged == [("Unhandled exception class: %s", "RuntimeError")]


def test_inbox_channel_filter_and_bulk_tags(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    rest = post(client, token, {"event_type": "knowledge_gap", "question": "R", "tags": ["rest"]})
    assert rest.status_code == 202
    with app.state.session_factory() as db:
        principal, _ = authenticate_bearer(db, app.state.settings, token)
    mcp = anyio.run(
        lambda: ingest(
            app.state.session_factory,
            app.state.settings,
            app.state.feedback_limiter,
            principal,
            {"event_type": "knowledge_gap", "question": "M", "tags": ["mcp"]},
            None,
            submitted_via="mcp",
            client_ip="127.0.0.1",
        )
    )
    statements: list[str] = []

    def track(
        _conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any
    ) -> None:
        statements.append(statement.lower())

    event.listen(app.state.engine, "before_cursor_execute", track)
    try:
        all_rows = client.get("/api/v1/admin/feedback")
    finally:
        event.remove(app.state.engine, "before_cursor_execute", track)
    assert all_rows.status_code == 200
    assert {item["submitted_via"] for item in all_rows.json()["items"]} == {"rest", "mcp"}
    assert (
        sum(
            "from feedback_event_tags" in sql or "join feedback_event_tags" in sql
            for sql in statements
        )
        == 1
    )
    assert [
        item["id"]
        for item in client.get("/api/v1/admin/feedback?submitted_via=mcp").json()["items"]
    ] == [mcp.event_id]
    assert [
        item["id"]
        for item in client.get("/api/v1/admin/feedback?submitted_via=rest").json()["items"]
    ] == [rest.json()["event_id"]]
    before = counts(app)
    assert client.get("/api/v1/admin/feedback?submitted_via=other").status_code == 422
    assert counts(app) == before
