"""Durable representative backfill and read-only calibration boundaries."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from kebivo.cli import main
from kebivo.dedup.evaluation import corpus, evaluate_project
from kebivo.models import (
    DedupEvaluation,
    EmbeddingBackfill,
    FeedbackEvent,
    FeedbackGroup,
    LlmProviderConfig,
    ProcessingJob,
    Project,
    QuestionEmbedding,
)
from kebivo.tests.test_embedding_matching import enable_embeddings
from kebivo.tests.test_feedback_submission import post
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_semantic_dedup import setup_semantic
from kebivo.worker.runner import Worker

pytest_plugins = ["kebivo.tests.test_semantic_dedup"]


def test_backfill_restart_batches_pause_and_prune(
    tmp_path: Path, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import fake_llm_server

    original_embedding = fake_llm_server._embedding
    monkeypatch.setattr(
        fake_llm_server, "_embedding", lambda value: original_embedding(value) + [0.0] * 704
    )
    client, app, project, token = setup_semantic(tmp_path, fake)
    for index in range(18):
        response = post(
            client,
            token,
            {"event_type": "knowledge_gap", "question": f"Question {index} about topic {index}"},
        )
        assert response.status_code == 202
    enable_embeddings(client, app, project)
    enable_embeddings(client, app, project)
    with app.state.session_factory() as db:
        provider_id = db.scalar(select(LlmProviderConfig.id))
        assert db.scalar(select(func.count()).select_from(EmbeddingBackfill)) == 1
        assert db.get(EmbeddingBackfill, project["id"]).dims is None
        groups_before = db.scalar(select(func.count()).select_from(FeedbackGroup))
    worker = Worker(app.state.session_factory, app.state.settings)
    worker.provider_failures[provider_id] = (2, time.monotonic() + 60)
    asyncio.run(worker._backfill())
    assert not fake.requests
    worker.provider_failures.clear()
    asyncio.run(worker._backfill())
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 16
        job = db.get(EmbeddingBackfill, project["id"])
        assert job is not None and job.remaining == 1984
        assert job.dims == 768
        first = db.scalar(select(QuestionEmbedding))
        assert first is not None
        first.model = "stale-model"
        db.commit()
    restarted = Worker(app.state.session_factory, app.state.settings)
    asyncio.run(restarted._backfill())
    asyncio.run(restarted._backfill())
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 17
        assert db.get(EmbeddingBackfill, project["id"]) is None
        assert db.scalar(select(func.count()).select_from(FeedbackGroup)) == groups_before
        assert (
            db.scalar(
                select(func.count())
                .select_from(QuestionEmbedding)
                .where(QuestionEmbedding.model == "stale-model")
            )
            == 0
        )
    assert [len(json.loads(request["body"])["input"]) for request in fake.requests] == [16, 2]


def test_backfill_and_event_embedding_same_vector_complete(tmp_path: Path, fake: Any) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    enable_embeddings(client, app, project)
    event_id = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access now"}
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    batch = worker._backfill_batch()
    assert batch is not None
    backfill, _, _, pending = batch
    assert [item[1] for item in pending] == [event_id]

    with app.state.session_factory() as db:
        job_id = db.scalar(select(ProcessingJob.id).where(ProcessingJob.event_id == event_id))
    assert job_id is not None and worker._claim("job", 1) == [job_id]
    work = worker._prepare_semantic(job_id)
    assert work is not None
    asyncio.run(worker._ask_embedding(work))
    assert work.vector is not None

    worker._finish_backfill_batch(
        backfill.project_id,
        backfill.provider_id,
        backfill.embedding_revision,
        backfill.model,
        backfill.dims,
        pending,
        [work.vector],
    )
    worker._process_sync(job_id, work)
    with app.state.session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        assert job is not None and job.status == "completed" and job.error_code is None
        assert db.get(FeedbackEvent, event_id).processing_status == "completed"
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 1
        assert db.scalar(select(func.count()).select_from(DedupEvaluation)) == 1


def test_inflight_backfill_does_not_write_after_provider_edit(tmp_path: Path, fake: Any) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    semantic = enable_embeddings(client, app, project)
    event_id = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access"}
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    batch = worker._backfill_batch()
    assert batch is not None
    old_job, _, _, pending = batch
    changed = client.patch(
        f"/api/v1/admin/llm-providers/{semantic['provider_id']}",
        json={"base_url": f"http://localhost:{fake.server_port}"},
        headers=write_headers(client),
    )
    assert changed.status_code == 200, changed.text
    worker._finish_backfill_batch(
        old_job.project_id,
        old_job.provider_id,
        old_job.embedding_revision,
        old_job.model,
        old_job.dims,
        pending,
        [[1.0] * 64],
    )
    with app.state.session_factory() as db:
        assert db.get(QuestionEmbedding, event_id) is None
        current = db.get(EmbeddingBackfill, project["id"])
        assert current is not None and current.embedding_revision != old_job.embedding_revision


def test_embedding_job_retries_after_routing_error(tmp_path: Path, fake: Any) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    enable_embeddings(client, app, project)
    event_id = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access now"}
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    job_id = worker._claim("job", 1)[0]
    worker._process_failed(job_id)
    with app.state.session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        assert job is not None and job.status == "pending"
        assert job.error_code == "routing_error" and job.attempt_count == 1
        assert job.next_attempt_at is not None
        job.next_attempt_at = None
        db.commit()
    assert worker._claim("job", 1) == [job_id]
    asyncio.run(worker._process(job_id))
    with app.state.session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        assert job is not None and job.status == "completed" and job.error_code is None
        assert db.get(FeedbackEvent, event_id).processing_status == "completed"
        assert db.get(DedupEvaluation, event_id) is not None


def test_embedding_provider_test_pass_and_fail(tmp_path: Path, fake: Any) -> None:
    client, app, project, _ = setup_semantic(tmp_path, fake)
    listed = client.get("/api/v1/admin/llm-providers", headers=write_headers(client))
    assert listed.json()["items"][0]["used_for_embeddings"] is False
    enable_embeddings(client, app, project)
    listed = client.get("/api/v1/admin/llm-providers", headers=write_headers(client))
    assert listed.json()["items"][0]["used_for_embeddings"] is True
    with app.state.session_factory() as db:
        provider_id = db.scalar(select(LlmProviderConfig.id))
    path = f"/api/v1/admin/llm-providers/{provider_id}/test"
    passed = client.post(path + "?kind=embeddings", headers=write_headers(client))
    assert passed.status_code == 200 and passed.json()["ok"] is True
    assert passed.json()["dims"] == 64
    assert passed.json()["related_similarity"] > passed.json()["unrelated_similarity"]
    assert json.loads(fake.requests[-1]["body"])["model"] == "fake-64"
    fake.scenario = "malformed"
    failed = client.post(path + "?kind=embeddings", headers=write_headers(client))
    assert failed.status_code == 200 and failed.json()["error_category"] == "invalid_response"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 0


def test_calibration_script_scores_corpus_against_fake_server(fake: Any) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/calibrate_embeddings.py",
            "--url",
            f"http://127.0.0.1:{fake.server_port}",
            "--model",
            "fake-64",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "model=fake-64 pairs=150" in result.stdout
    assert "threshold precision recall" in result.stdout


@pytest.mark.parametrize("scenario", ["malformed", "429"])
def test_backfill_failed_batch_waits_for_cooldown(tmp_path: Path, fake: Any, scenario: str) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    assert (
        post(
            client, token, {"event_type": "knowledge_gap", "question": "Reset my password"}
        ).status_code
        == 202
    )
    enable_embeddings(client, app, project)
    worker = Worker(app.state.session_factory, app.state.settings)
    fake.scenario = scenario
    asyncio.run(worker._backfill())
    assert len(fake.requests) == 1
    assert worker.backfill_retry_at[project["id"]] > time.monotonic()
    asyncio.run(worker._backfill())
    assert len(fake.requests) == 1


def test_corpus_balanced_and_eval_requires_acknowledged_provider(
    tmp_path: Path, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs = corpus()
    assert len(pairs) == 150 and sum(label for _, _, label in pairs) == 75
    client, app, project, _ = setup_semantic(tmp_path, fake)
    with app.state.session_factory() as db:
        row = db.get(Project, project["id"])
        assert row is not None
        with pytest.raises(ValueError, match="acknowledge privacy"):
            evaluate_project(db, app.state.settings, project["id"], True)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'feedback.db'}")
        monkeypatch.setattr(sys, "argv", ["kebivo", "dedup-eval", "--project", project["id"]])
        with pytest.raises(SystemExit, match="acknowledge privacy"):
            main()
        row.settings_json = json.dumps(
            {
                "deduplication": {
                    "mode": "semantic",
                    "privacy_acknowledged": True,
                    "semantic": {
                        "method": "embeddings",
                        "provider_id": "00000000-0000-0000-0000-000000000000",
                    },
                }
            }
        )
        db.commit()
        with pytest.raises(ValueError, match="provider is unavailable"):
            evaluate_project(db, app.state.settings, project["id"], True)
        row.settings_json = json.dumps({"deduplication": {"mode": "exact"}})
        db.commit()
        with pytest.raises(ValueError, match="acknowledge privacy"):
            evaluate_project(db, app.state.settings, project["id"], True)
