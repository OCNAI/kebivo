"""Embedding worker matching, settings, privacy and bounded scoring."""

import json
import time
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from kebivo.dedup.embeddings import encode
from kebivo.dedup.semantic import score_embeddings
from kebivo.models import DedupEvaluation, FeedbackGroupMember, QuestionEmbedding
from kebivo.tests.test_feedback_submission import post
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_semantic_dedup import process, setup_semantic

pytest_plugins = ["kebivo.tests.test_semantic_dedup"]


def enable_embeddings(client: Any, app: Any, project: dict[str, Any]) -> dict[str, Any]:
    with app.state.session_factory() as db:
        provider_id = db.scalar(select(QuestionEmbedding.provider_id))
        if provider_id is None:
            from kebivo.models import LlmProviderConfig

            provider_id = db.scalar(select(LlmProviderConfig.id))
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "settings": {
                "deduplication": {
                    "mode": "semantic",
                    "semantic": {
                        "method": "embeddings",
                        "provider_id": provider_id,
                        "embedding_model": "fake-64",
                        "embedding_dimensions": 64,
                    },
                    "privacy_acknowledged": True,
                }
            }
        },
        headers=write_headers(client),
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["settings"]["deduplication"]["semantic"])


def test_matching_preview_privacy_and_stale_model(
    tmp_path: Path, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import fake_llm_server

    original_embedding = fake_llm_server._embedding
    monkeypatch.setattr(
        fake_llm_server, "_embedding", lambda value: original_embedding(value) + [0.0] * 704
    )
    client, app, project, token = setup_semantic(tmp_path, fake)
    settings = enable_embeddings(client, app, project)
    assert "embedding_dimensions" not in settings
    assert settings["similarity_threshold"] == 0.93
    assert settings["max_scan"] == 2000
    defaults = client.get("/api/v1/projects/dedup/defaults").json()["embeddings"]
    assert defaults["max_scan_cap"] == 5000
    assert "embedding_dimensions" not in defaults
    first = post(
        client,
        token,
        {
            "event_type": "knowledge_gap",
            "question": "How can I reset my VPN?",
            "context": "SECRET_CANARY",
        },
    ).json()["event_id"]
    process(app, first)
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset the VPN?"}
    ).json()["event_id"]
    process(app, second)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, first).outcome == "no_candidates"
        evaluation = db.get(DedupEvaluation, second)
        assert evaluation.method == "embeddings" and evaluation.outcome == "matched"
        assert evaluation.reason is None and evaluation.confidence >= 0.88
        assert (
            db.get(FeedbackGroupMember, first).group_id
            == db.get(FeedbackGroupMember, second).group_id
        )
        assert db.get(QuestionEmbedding, second) is not None
        assert db.get(QuestionEmbedding, second).dims == 768
    assert all(request["path"] == "/embeddings" for request in fake.requests)
    assert all("SECRET_CANARY" not in request["body"] for request in fake.requests)
    assert all(len(json.loads(request["body"])["input"]) == 1 for request in fake.requests)
    with app.state.session_factory() as db:
        stored_before = db.scalar(select(func.count()).select_from(QuestionEmbedding))
    preview = client.post(
        f"/api/v1/admin/projects/{project['id']}/dedup/preview",
        json={"event_type": "knowledge_gap", "question": "How do I reset my VPN?"},
        headers=write_headers(client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["method"] == "embeddings"
    assert preview.json()["similarities"][0]["threshold_met"] is True
    assert preview.json()["similarities"][0]["question_excerpt"] == "How can I reset my VPN?"
    assert len(preview.json()["similarities"]) <= 5
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == stored_before
    third = post(
        client, token, {"event_type": "knowledge_gap", "question": "A different topic entirely"}
    ).json()["event_id"]
    process(app, third)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, third).outcome == "no_match"
        db.get(QuestionEmbedding, first).model = "stale-model"
        db.get(QuestionEmbedding, second).model = "stale-model"
        db.get(QuestionEmbedding, third).model = "stale-model"
        db.commit()
    fourth = post(
        client, token, {"event_type": "knowledge_gap", "question": "Another different topic"}
    ).json()["event_id"]
    process(app, fourth)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, fourth).outcome == "no_candidates"


@pytest.mark.parametrize(
    "bad",
    [
        {"similarity_threshold": 0.49},
        {"similarity_threshold": 1.0},
        {"max_scan": 0},
        {"max_scan": 5001},
        {"candidate_count": 2},
    ],
)
def test_embedding_settings_reject_invalid(tmp_path: Path, fake: Any, bad: dict[str, Any]) -> None:
    client, app, project, _ = setup_semantic(tmp_path, fake)
    enable_embeddings(client, app, project)
    from kebivo.models import LlmProviderConfig

    with app.state.session_factory() as db:
        provider_id = db.scalar(select(LlmProviderConfig.id))
    settings = {
        "method": "embeddings",
        "provider_id": provider_id,
        "embedding_model": "fake-64",
        "embedding_dimensions": 64,
        **bad,
    }
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "settings": {
                "deduplication": {
                    "mode": "semantic",
                    "semantic": settings,
                    "privacy_acknowledged": True,
                }
            }
        },
        headers=write_headers(client),
    )
    assert response.status_code == 422


def test_scoring_5000_by_768_under_two_seconds() -> None:
    vector = [1.0] + [0.0] * 767
    blob = encode(vector)
    rows = [(str(i), str(i), blob) for i in range(5000)]
    started = time.perf_counter()
    scores = score_embeddings(vector, rows)
    elapsed = time.perf_counter() - started
    assert len(scores) == 5
    assert elapsed <= 2.0, f"scoring took {elapsed:.3f}s"


def test_endpoint_failure_opens_circuit_and_keeps_delivery(tmp_path: Path, fake: Any) -> None:
    import asyncio

    from kebivo.models import (
        Delivery,
        Destination,
        FeedbackEvent,
        LlmProviderConfig,
        ProcessingJob,
        Project,
    )
    from kebivo.worker.runner import Worker

    client, app, project, token = setup_semantic(tmp_path, fake)
    enable_embeddings(client, app, project)
    with app.state.session_factory() as db:
        destination = Destination(name="Fallback inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        db.get(Project, project["id"]).fallback_destination_id = destination.id
        db.commit()
    worker = Worker(app.state.session_factory, app.state.settings)
    fake.scenario = "500"
    for index, expected in enumerate(("provider_error", "provider_unavailable")):
        event_id = post(
            client,
            token,
            {"event_type": "knowledge_gap", "question": f"Unique embedding question {index}"},
        ).json()["event_id"]
        with app.state.session_factory() as db:
            job_id = db.scalar(select(ProcessingJob.id).where(ProcessingJob.event_id == event_id))
        assert job_id is not None and job_id in worker._claim("job", 1)
        asyncio.run(worker._process(job_id))
        with app.state.session_factory() as db:
            assert db.get(DedupEvaluation, event_id).outcome == expected
            assert db.get(FeedbackEvent, event_id).processing_status == "completed"
            assert db.scalar(select(Delivery).where(Delivery.event_id == event_id)) is not None
            assert db.get(FeedbackGroupMember, event_id) is not None
            assert db.get(QuestionEmbedding, event_id) is None
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert worker.provider_failures[provider.id][0] == 2
    assert len(fake.requests) == 2


def test_embedding_group_touched_before_commit(tmp_path: Path, fake: Any) -> None:
    import asyncio

    from kebivo.models import FeedbackGroup, ProcessingJob
    from kebivo.worker.runner import Worker

    client, app, project, token = setup_semantic(tmp_path, fake)
    enable_embeddings(client, app, project)
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset my VPN?"}
    ).json()["event_id"]
    process(app, first)
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset the VPN?"}
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    with app.state.session_factory() as db:
        job_id = db.scalar(select(ProcessingJob.id).where(ProcessingJob.event_id == second))
    assert job_id is not None and job_id in worker._claim("job", 1)
    work = worker._prepare_semantic(job_id)
    assert work is not None
    asyncio.run(worker._ask_embedding(work))
    with app.state.session_factory() as db:
        group = db.get(FeedbackGroup, work.group_id)
        group.status = "resolved"
        db.commit()
    worker._process_sync(job_id, work)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, second).outcome == "skipped_group_changed"
        assert (
            db.get(FeedbackGroupMember, second).group_id
            != db.get(FeedbackGroupMember, first).group_id
        )


def test_method_switch_requires_ack_and_rejects_azure(tmp_path: Path, fake: Any) -> None:
    from kebivo.models import LlmProviderConfig

    client, app, project, _ = setup_semantic(tmp_path, fake)
    with app.state.session_factory() as db:
        provider_id = db.scalar(select(LlmProviderConfig.id))
    semantic = {
        "method": "embeddings",
        "provider_id": provider_id,
        "embedding_model": "fake-64",
        "embedding_dimensions": 64,
    }
    path = f"/api/v1/projects/{project['id']}"
    no_ack = client.patch(
        path,
        json={"settings": {"deduplication": {"mode": "semantic", "semantic": semantic}}},
        headers=write_headers(client),
    )
    assert no_ack.status_code == 422
    with app.state.session_factory() as db:
        provider = db.get(LlmProviderConfig, provider_id)
        provider.provider_type = "azure_openai"
        db.commit()
    azure = client.patch(
        path,
        json={
            "settings": {
                "deduplication": {
                    "mode": "semantic",
                    "semantic": semantic,
                    "privacy_acknowledged": True,
                }
            }
        },
        headers=write_headers(client),
    )
    assert azure.status_code == 422
    assert azure.json()["error"]["code"] == "invalid_provider"
