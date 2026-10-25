"""Feedback acceptance, isolation, idempotency and durability contracts."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from kebivo.app import create_app
from kebivo.feedback.routes import CORRECTION_EXAMPLE, GAP_EXAMPLE
from kebivo.models import (
    FeedbackEvent,
    FeedbackEventTag,
    IdempotencyRecord,
    ProcessingJob,
    Tag,
    now_utc,
)
from kebivo.settings import Settings
from kebivo.tests.test_projects import make_client
from kebivo.tests.test_tokens import create_project, issue_token
from kebivo.tokens import CORRECTION_SCOPE, GAP_SCOPE


def setup(
    tmp_path: Path, *, corrections: bool = True
) -> tuple[TestClient, FastAPI, dict[str, Any], str]:
    client, app = make_client(tmp_path / "feedback.db")
    project = create_project(client, "alpha", corrections=corrections)
    token = issue_token(client, project["id"], "agent", [GAP_SCOPE, CORRECTION_SCOPE]).json()[
        "token"
    ]
    return client, app, project, token


def post(
    client: TestClient, token: str | None, payload: dict[str, Any], key: str | None = None
) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/api/v1/feedback", json=payload, headers=headers)


def counts(app: FastAPI) -> tuple[int, ...]:
    with app.state.session_factory() as db:
        return tuple(
            int(db.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                FeedbackEvent,
                FeedbackEventTag,
                IdempotencyRecord,
                ProcessingJob,
                Tag,
            )
        )


def test_one_post_union_and_file_restart(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    with app.state.session_factory() as db:
        db.add(Tag(project_id=project["id"], name="IT", slug="it", is_default=True))
        db.add(Tag(project_id=project["id"], name="Internal", slug="internal", is_default=True))
        db.commit()
    response = post(client, token, GAP_EXAMPLE, "one")
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    event_id = response.json()["event_id"]
    with app.state.session_factory() as db:
        event = db.get(FeedbackEvent, event_id)
        assert event is not None and event.question == GAP_EXAMPLE["question"]
        assert event.question_hash_version == 2 and len(event.normalized_question_hash) == 64
        links = db.scalars(
            select(FeedbackEventTag).where(FeedbackEventTag.event_id == event_id)
        ).all()
        assert len(links) == 2 and all(link.from_default and link.from_submission for link in links)
        assert (
            db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id)).status
            == "pending"
        )
    restarted = create_app(
        Settings(
            "test", "test", "project-tests-secret-key-000000000000", str(tmp_path / "feedback.db")
        )
    )
    with restarted.state.session_factory() as db:
        assert db.get(FeedbackEvent, event_id) is not None
        assert (
            db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id)) is not None
        )
    restarted.state.engine.dispose()


def test_idempotency_matrix_and_project_scope(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    first = post(client, token, GAP_EXAMPLE, "shared")
    assert first.status_code == 202
    equivalent = {
        **GAP_EXAMPLE,
        "question": "  " + GAP_EXAMPLE["question"] + "  ",
        "tags": ["internal", "IT", "it"],
    }
    replay = post(client, token, equivalent, "shared")
    assert replay.status_code == 202 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["event_id"] == first.json()["event_id"]
    conflict = post(client, token, {**GAP_EXAMPLE, "question": "Changed"}, "shared")
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    )
    assert counts(app)[:4] == (1, 2, 1, 1)
    other = create_project(client, "beta")
    other_token = issue_token(client, other["id"], "agent").json()["token"]
    cross = post(client, other_token, GAP_EXAMPLE, "shared")
    assert cross.status_code == 202 and cross.json()["event_id"] != first.json()["event_id"]
    with app.state.session_factory() as db:
        record = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.key == "shared", IdempotencyRecord.project_id == other["id"]
            )
        )
        assert record is not None
        record.expires_at = now_utc() - timedelta(seconds=1)
        db.commit()
    expired = post(client, other_token, GAP_EXAMPLE, "shared")
    assert expired.status_code == 202 and expired.json()["event_id"] != cross.json()["event_id"]
    assert post(client, token, GAP_EXAMPLE).json()["event_id"] != first.json()["event_id"]


def test_concurrent_same_key_threads(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)

    def send(_: int) -> Response:
        return post(client, token, GAP_EXAMPLE, "parallel")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(send, range(6)))
    assert all(item.status_code == 202 for item in responses)
    assert len({item.json()["event_id"] for item in responses}) == 1
    assert counts(app)[:4] == (1, 2, 1, 1)


def test_twenty_tags_accepted(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    payload = {
        "event_type": "knowledge_gap",
        "question": "Twenty distinct tags",
        "tags": [f"t{number:05x}" for number in range(20)],
    }
    response = post(client, token, payload)
    assert response.status_code == 202
    assert counts(app)[:2] == (1, 20)


def test_twenty_one_tags_rejected_without_writes(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    before = counts(app)
    response = post(
        client,
        token,
        {
            "event_type": "knowledge_gap",
            "question": "Too many tags",
            "tags": [f"tag{i}" for i in range(21)],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["fields"][0]["field"] == "tags"
    assert counts(app) == before


@pytest.mark.parametrize(
    "payload",
    [
        {**GAP_EXAMPLE, "project_id": "other"},
        {**GAP_EXAMPLE, "question": "x" * 10001},
        {**GAP_EXAMPLE, "context": "x" * 4001},
        {**GAP_EXAMPLE, "agent_id": "x" * 129},
        {**GAP_EXAMPLE, "conversation_id": "x" * 257},
        {**GAP_EXAMPLE, "tags": ["bad!"]},
        {**GAP_EXAMPLE, "metadata": {"x": "a" * 8193}},
        {**GAP_EXAMPLE, "metadata": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": 1}}}}}}}}},
        {**CORRECTION_EXAMPLE, "correction": "x" * 20001},
        {**CORRECTION_EXAMPLE, "source_reference": "x" * 2001},
    ],
)
def test_boundary_rejection_writes_nothing(tmp_path: Path, payload: dict[str, Any]) -> None:
    client, app, _, token = setup(tmp_path)
    before = counts(app)
    response = post(client, token, payload)
    assert response.status_code == 422
    assert counts(app) == before


def test_field_values_at_limits_are_accepted_without_truncation(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    metadata_text = "m" * 8_184
    gap: dict[str, Any] = {
        "event_type": "knowledge_gap",
        "question": "q" * 10_000,
        "previous_answer": "p" * 10_000,
        "context": "c" * 4_000,
        "agent_id": "a" * 128,
        "conversation_id": "v" * 256,
        "source_event_id": "e" * 256,
        "user_reference": "u" * 256,
        "metadata": {"k": metadata_text},
    }
    accepted_gap = post(client, token, gap)
    assert accepted_gap.status_code == 202
    with app.state.session_factory() as db:
        stored_gap = db.get(FeedbackEvent, accepted_gap.json()["event_id"])
        assert stored_gap is not None
        assert stored_gap.question == gap["question"]
        assert stored_gap.previous_answer == gap["previous_answer"]
        assert stored_gap.context == gap["context"]
        assert stored_gap.agent_id == gap["agent_id"]
        assert stored_gap.conversation_id == gap["conversation_id"]
        assert stored_gap.source_event_id == gap["source_event_id"]
        assert stored_gap.user_reference == gap["user_reference"]
        assert stored_gap.metadata_json == '{"k":"' + metadata_text + '"}'

    correction = {
        "event_type": "knowledge_correction",
        "question": "q" * 10_000,
        "correction": "c" * 20_000,
        "previous_answer": "p" * 10_000,
        "source_reference": "r" * 2_000,
    }
    accepted_correction = post(client, token, correction)
    assert accepted_correction.status_code == 202
    with app.state.session_factory() as db:
        stored_correction = db.get(FeedbackEvent, accepted_correction.json()["event_id"])
        assert stored_correction is not None
        assert stored_correction.question == correction["question"]
        assert stored_correction.correction == correction["correction"]
        assert stored_correction.previous_answer == correction["previous_answer"]
        assert stored_correction.source_reference == correction["source_reference"]


def test_auth_policy_and_bad_json_write_nothing(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path, corrections=False)
    gap_only = issue_token(client, project["id"], "gap").json()["token"]
    before = counts(app)
    assert post(client, None, GAP_EXAMPLE).status_code == 401
    assert post(client, "invalid", GAP_EXAMPLE).status_code == 401
    assert post(client, gap_only, CORRECTION_EXAMPLE).status_code == 403
    assert post(client, token, CORRECTION_EXAMPLE).status_code == 403
    assert post(client, token, GAP_EXAMPLE, "bad key").status_code == 400
    assert (
        client.post(
            "/api/v1/feedback",
            content=b"{",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        ).status_code
        == 400
    )
    assert counts(app) == before
    # A built frontend's GET fallback returns 404; without it, the POST route returns 405.
    assert client.get(
        "/api/v1/feedback", headers={"Authorization": f"Bearer {token}"}
    ).status_code in {404, 405}
    assert counts(app) == before
    with TestClient(app) as bearer_only:
        assert (
            bearer_only.get(
                "/api/v1/projects", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )
        assert (
            bearer_only.get(
                "/api/v1/admin/feedback", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )


def test_prd_correction_and_log_redaction(tmp_path: Path) -> None:
    client, app, _, token = setup(tmp_path)
    logger = logging.getLogger("kebivo.request")
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    stream = StringIO()
    old = handler.stream
    handler.setStream(stream)
    try:
        response = post(client, token, CORRECTION_EXAMPLE, "private-key")
    finally:
        handler.setStream(old)
    assert response.status_code == 202
    with app.state.session_factory() as db:
        event = db.get(FeedbackEvent, response.json()["event_id"])
        assert event is not None and event.correction == CORRECTION_EXAMPLE["correction"]
    for private in (
        CORRECTION_EXAMPLE["question"],
        CORRECTION_EXAMPLE["correction"],
        token,
        "private-key",
    ):
        assert private not in stream.getvalue()


def test_rejected_credentials_and_rate_limit_do_not_write(tmp_path: Path) -> None:
    from dataclasses import replace

    from kebivo.feedback_limits import FixedWindowLimiter
    from kebivo.models import ApiToken, Project

    client, app, project, token = setup(tmp_path)
    before = counts(app)
    with app.state.session_factory() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.project_id == project["id"]))
        assert row is not None
        row.revoked_at = now_utc()
        db.commit()
    assert post(client, token, GAP_EXAMPLE).status_code == 401
    assert counts(app) == before
    with app.state.session_factory() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.project_id == project["id"]))
        assert row is not None
        row.revoked_at = None
        row.expires_at = now_utc() - timedelta(seconds=1)
        db.commit()
    assert post(client, token, GAP_EXAMPLE).status_code == 401
    assert counts(app) == before
    with app.state.session_factory() as db:
        row = db.scalar(select(ApiToken).where(ApiToken.project_id == project["id"]))
        assert row is not None
        row.expires_at = None
        project_row = db.get(Project, project["id"])
        assert project_row is not None
        project_row.is_active = False
        db.commit()
    assert post(client, token, GAP_EXAMPLE).status_code == 401
    assert counts(app) == before
    with app.state.session_factory() as db:
        project_row = db.get(Project, project["id"])
        assert project_row is not None
        project_row.is_active = True
        db.commit()
    app.state.settings = replace(app.state.settings, feedback_rate_limit_per_token=1)
    app.state.feedback_limiter = FixedWindowLimiter()
    assert post(client, token, GAP_EXAMPLE).status_code == 202
    accepted = counts(app)
    limited = post(client, token, GAP_EXAMPLE)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert counts(app) == accepted
