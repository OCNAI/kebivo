"""Durable representative backfill and read-only calibration boundaries."""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from kebivo.cli import main
from kebivo.dedup.evaluation import corpus, evaluate_project
from kebivo.models import (
    EmbeddingBackfill,
    FeedbackGroup,
    LlmProviderConfig,
    Project,
    QuestionEmbedding,
)
from kebivo.tests.test_embedding_matching import enable_embeddings
from kebivo.tests.test_feedback_submission import post
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_semantic_dedup import setup_semantic
from kebivo.worker.runner import Worker

pytest_plugins = ["kebivo.tests.test_semantic_dedup"]


def test_backfill_restart_batches_pause_and_prune(tmp_path: Path, fake: Any) -> None:
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


def test_embedding_provider_test_pass_and_fail(tmp_path: Path, fake: Any) -> None:
    client, app, _, _ = setup_semantic(tmp_path, fake)
    with app.state.session_factory() as db:
        provider_id = db.scalar(select(LlmProviderConfig.id))
    path = f"/api/v1/admin/llm-providers/{provider_id}/test?kind=embeddings&dimensions=64"
    passed = client.post(path, headers=write_headers(client))
    assert passed.status_code == 200 and passed.json()["ok"] is True
    assert passed.json()["dims"] == 64
    fake.scenario = "malformed"
    failed = client.post(path, headers=write_headers(client))
    assert failed.status_code == 200 and failed.json()["ok"] is False
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 0


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
