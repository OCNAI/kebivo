"""Semantic worker boundaries against the local scripted provider."""

import asyncio
import json
import random
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from kebivo.dedup.semantic import candidates
from kebivo.feedback.groups import check_integrity
from kebivo.models import (
    DedupEvaluation,
    Delivery,
    Destination,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupMember,
    LlmProviderConfig,
    ProcessingJob,
    Project,
)
from kebivo.tests.test_db import _alembic_config
from kebivo.tests.test_feedback_submission import post, setup
from kebivo.tests.test_llm_providers import payload
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_tokens import create_project, issue_token
from kebivo.worker.runner import Worker
from scripts.fake_llm_server import FakeLLMServer


@pytest.fixture
def fake() -> Any:
    server = FakeLLMServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def setup_semantic(tmp_path: Path, fake: FakeLLMServer) -> Any:
    command.upgrade(_alembic_config(tmp_path / "feedback.db"), "head")
    client, app, project, token = setup(tmp_path)
    headers = write_headers(client)
    provider = client.post(
        "/api/v1/admin/llm-providers",
        json=payload(f"http://127.0.0.1:{fake.server_port}"),
        headers=headers,
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["id"]
    changed = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "settings": {
                "deduplication": {
                    "mode": "semantic",
                    "semantic": {"provider_id": provider_id},
                    "privacy_acknowledged": True,
                }
            }
        },
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    return client, app, project, token


def process(app: Any, event_id: str) -> None:
    worker = Worker(app.state.session_factory, app.state.settings)
    with app.state.session_factory() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        assert job is not None
        job_id = job.id
    assert worker._claim("job", 1)
    asyncio.run(worker._process(job_id))


def test_semantic_match_privacy_and_no_model_shortcuts(tmp_path: Path, fake: FakeLLMServer) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    first = post(
        client,
        token,
        {
            "event_type": "knowledge_gap",
            "question": "How can I reset my VPN?",
            "context": "CONTEXT_CANARY",
        },
    ).json()["event_id"]
    process(app, first)
    assert len(fake.requests) == 0
    second = post(
        client,
        token,
        {
            "event_type": "knowledge_gap",
            "question": "Reset VPN access please",
            "context": "NEW_CONTEXT_CANARY",
            "previous_answer": "ANSWER_CANARY",
            "metadata": {"secret": "METADATA_CANARY"},
            "tags": ["TAG_CANARY"],
            "agent_id": "AGENT_CANARY",
            "conversation_id": "CONVERSATION_CANARY",
            "user_reference": "USER_CANARY",
        },
    ).json()["event_id"]
    process(app, second)
    with app.state.session_factory() as db:
        first_group = db.get(FeedbackGroupMember, first).group_id
        second_member = db.get(FeedbackGroupMember, second)
        evaluation = db.get(DedupEvaluation, second)
        assert second_member.group_id == first_group
        assert second_member.match_method == "semantic"
        assert evaluation.outcome == "matched"
        assert check_integrity(db) == []
    assert len(fake.requests) == 1
    body = fake.requests[0]["body"]
    assert all(
        canary not in body
        for canary in (
            "NEW_CONTEXT_CANARY",
            "ANSWER_CANARY",
            "METADATA_CANARY",
            "TAG_CANARY",
            "AGENT_CANARY",
            "CONVERSATION_CANARY",
            "USER_CANARY",
            first_group,
        )
    )
    exact = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset my VPN"}
    ).json()["event_id"]
    process(app, exact)
    assert len(fake.requests) == 1
    assert client.get(f"/api/v1/admin/groups/{first_group}/similar").status_code == 200
    assert (
        client.get(f"/api/v1/admin/groups/{first_group}/events").json()["items"][1]["semantic"][
            "outcome"
        ]
        == "matched"
    )
    preview = client.post(
        f"/api/v1/admin/projects/{project['id']}/dedup/preview",
        json={"event_type": "knowledge_gap", "question": "How can I reset my VPN?"},
        headers=write_headers(client),
    )
    assert preview.status_code == 200
    assert preview.json()["exact_match_group_id"] == first_group
    assert len(fake.requests) == 1
    assert (
        client.post(
            f"/api/v1/admin/projects/{project['id']}/dedup/preview",
            json={"event_type": "knowledge_gap", "question": "Another VPN question"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/admin/groups/{first_group}/similar",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 401
    )


def test_preview_respects_llm_capacity(tmp_path: Path, fake: FakeLLMServer) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    post(client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access"})
    app.state.llm_semaphore = asyncio.Semaphore(0)
    preview = client.post(
        f"/api/v1/admin/projects/{project['id']}/dedup/preview",
        json={"event_type": "knowledge_gap", "question": "How do I reset VPN access?"},
        headers=write_headers(client),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["verdict"] == {"outcome": "timeout"}
    assert fake.requests == []
    app.state.engine.dispose()


@pytest.mark.parametrize(
    "scenario,outcome",
    [
        ("500", "provider_error"),
        ("429", "provider_error"),
        ("malformed", "invalid_response"),
        ("wrong_label", "invalid_candidate"),
    ],
)
def test_semantic_failure_still_routes(
    tmp_path: Path, fake: FakeLLMServer, scenario: str, outcome: str
) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    with app.state.session_factory() as db:
        destination = Destination(name="Fallback inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        db.get(Project, project["id"]).fallback_destination_id = destination.id
        db.commit()
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset my VPN?"}
    ).json()["event_id"]
    process(app, first)
    fake.scenario = scenario
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access please"}
    )
    assert second.status_code == 202
    event_id = second.json()["event_id"]
    process(app, event_id)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, event_id).outcome == outcome
        assert db.get(FeedbackEvent, event_id).processing_status == "completed"
        assert db.scalar(select(Delivery).where(Delivery.event_id == event_id)) is not None
        assert (
            db.get(FeedbackGroupMember, event_id).group_id
            != db.get(FeedbackGroupMember, first).group_id
        )
        assert check_integrity(db) == []


def test_disabled_provider_and_changed_group_keep_routing(
    tmp_path: Path, fake: FakeLLMServer
) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset my VPN?"}
    ).json()["event_id"]
    process(app, first)
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access please"}
    ).json()["event_id"]
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert provider is not None
        provider.enabled = False
        db.commit()
    process(app, second)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, second).outcome == "provider_unavailable"
        assert db.get(FeedbackEvent, second).processing_status == "completed"
    assert len(fake.requests) == 0
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert provider is not None
        provider.enabled = True
        db.commit()
    third = post(
        client, token, {"event_type": "knowledge_gap", "question": "Resetting access for the VPN"}
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)
    job_id = worker._claim("job", 1)[0]
    work = worker._prepare_semantic(job_id)
    assert work is not None and work.candidates
    concurrent = post(
        client, token, {"event_type": "knowledge_gap", "question": "Resetting access for the VPN"}
    ).json()["event_id"]
    with app.state.session_factory() as db:
        group = db.get(FeedbackGroup, db.get(FeedbackGroupMember, third).group_id)
        assert group is not None and group.occurrence_count == 2
        assert db.get(FeedbackGroupMember, concurrent).group_id == group.id
    asyncio.run(worker._ask_semantic(work))
    worker._process_sync(job_id, work)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, third).outcome == "skipped_group_changed"
        assert db.get(FeedbackEvent, third).processing_status == "completed"
        assert db.get(FeedbackGroup, group.id) is not None
        assert check_integrity(db) == []


@pytest.mark.parametrize(
    "failure,outcome",
    [
        ("refused", "provider_error"),
        ("tls", "provider_error"),
        ("secret", "provider_unavailable"),
        ("timeout", "timeout"),
    ],
)
def test_transport_and_secret_failures_fall_back(
    tmp_path: Path, fake: FakeLLMServer, failure: str, outcome: str
) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How can I reset my VPN?"}
    ).json()["event_id"]
    process(app, first)
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert provider is not None
        if failure == "refused":
            provider.base_url = "http://127.0.0.1:1"
        elif failure == "tls":
            provider.base_url = f"https://127.0.0.1:{fake.server_port}"
        elif failure == "secret":
            provider.secrets_ciphertext = "v1:broken:blob:blob"
        else:
            provider.timeout_seconds = 1
            fake.scenario = "slow"
        db.commit()
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "Reset VPN access please"}
    )
    assert second.status_code == 202
    event_id = second.json()["event_id"]
    process(app, event_id)
    with app.state.session_factory() as db:
        assert db.get(DedupEvaluation, event_id).outcome == outcome
        assert db.get(FeedbackEvent, event_id).processing_status == "completed"
        assert check_integrity(db) == []


def test_hung_provider_does_not_starve_exact_jobs_or_deliveries(
    tmp_path: Path, fake: FakeLLMServer
) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    seed = post(
        client, token, {"event_type": "knowledge_gap", "question": "How do I reset VPN access?"}
    ).json()["event_id"]
    process(app, seed)
    fake.scenario = "slow"
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert provider is not None
        provider.timeout_seconds = 1
        semantic_project = db.get(Project, project["id"])
        assert semantic_project is not None
        settings = json.loads(semantic_project.settings_json)
        settings["deduplication"]["semantic"]["max_attempts"] = 1
        semantic_project.settings_json = json.dumps(settings)
        destination = Destination(name="Pending inbox", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        delivery = Delivery(
            event_id=seed,
            project_id=project["id"],
            destination_id=destination.id,
            status="pending",
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
    semantic_ids = [
        post(
            client,
            token,
            {"event_type": "knowledge_gap", "question": f"Reset VPN access for user {index}"},
        ).json()["event_id"]
        for index in range(12)
    ]
    exact_project = create_project(client, "exact-lane")
    exact_token = issue_token(client, exact_project["id"], "exact-agent", ["feedback:gap"]).json()[
        "token"
    ]
    exact_id = post(
        client,
        exact_token,
        {"event_type": "knowledge_gap", "question": "Unrelated exact project event"},
    ).json()["event_id"]
    worker = Worker(app.state.session_factory, app.state.settings)

    async def run() -> None:
        started = time.monotonic()
        await worker.start()
        try:
            async def done() -> None:
                while True:
                    with app.state.session_factory() as db:
                        exact = db.get(FeedbackEvent, exact_id)
                        sent = db.get(Delivery, delivery_id)
                        if exact.processing_status == "completed" and sent.status == "delivered":
                            return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(done(), timeout=3)
            assert time.monotonic() - started < 3

            async def semantic_done() -> None:
                while True:
                    with app.state.session_factory() as db:
                        if all(db.get(FeedbackEvent, event_id).processing_status == "completed"
                               for event_id in semantic_ids):
                            return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(semantic_done(), timeout=8)
        finally:
            await worker.stop()

    asyncio.run(run())
    with app.state.session_factory() as db:
        unavailable = [
            event_id for event_id in semantic_ids
            if db.get(DedupEvaluation, event_id).outcome == "provider_unavailable"
        ]
        assert unavailable
        for event_id in unavailable:
            event = db.get(FeedbackEvent, event_id)
            member = db.get(FeedbackGroupMember, event_id)
            assert event.processing_status == "completed"
            assert member.match_method == "initial"
            assert db.get(FeedbackGroup, member.group_id).representative_event_id == event_id
        assert check_integrity(db) == []
    # Already in-flight calls can finish after the circuit opens.
    assert len(fake.requests) <= 4


def test_migration_0010_to_0011(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    config = _alembic_config(path)
    command.upgrade(config, "0010_llm_providers")
    command.upgrade(config, "0011_semantic_dedup")
    from kebivo.db import make_engine

    engine = make_engine(f"sqlite:///{path}")
    try:
        assert "dedup_evaluations" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM dedup_evaluations")).scalar_one() == 0
            )
        command.downgrade(config, "0010_llm_providers")
        assert "dedup_evaluations" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_seeded_modes_keep_group_integrity(tmp_path: Path, fake: FakeLLMServer) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    rng = random.Random(117)
    for index in range(18):
        mode = ("disabled", "exact", "semantic")[index % 3]
        dedup: dict[str, Any] = (
            {"mode": mode}
            if mode != "semantic"
            else {
                "mode": "semantic",
                "semantic": {"provider_id": next_provider_id(app)},
                "privacy_acknowledged": True,
            }
        )
        changed = client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"settings": {"deduplication": dedup}},
            headers=write_headers(client),
        )
        assert changed.status_code == 200
        topic = rng.choice(("VPN", "email", "billing"))
        subject = rng.choice(("access", "login"))
        question = f"How can I reset {topic} {subject}?"
        response = post(client, token, {"event_type": "knowledge_gap", "question": question})
        assert response.status_code == 202
        process(app, response.json()["event_id"])
        with app.state.session_factory() as db:
            assert check_integrity(db) == [], index


def test_candidates_stay_inside_project_and_type(tmp_path: Path, fake: FakeLLMServer) -> None:
    client, app, project, token = setup_semantic(tmp_path, fake)
    other = create_project(client, "other", corrections=True)
    other_token = issue_token(client, other["id"], "other", ["feedback:gap"]).json()["token"]
    question = "Where is VPN access?"
    own = post(client, token, {"event_type": "knowledge_gap", "question": question}).json()[
        "event_id"
    ]
    post(
        client,
        token,
        {
            "event_type": "knowledge_correction",
            "question": question,
            "correction": "CORRECTION_CANARY",
        },
    )
    post(client, other_token, {"event_type": "knowledge_gap", "question": question})
    with app.state.session_factory() as db:
        own_group = db.get(FeedbackGroupMember, own).group_id
        hits = candidates(db, project["id"], "knowledge_gap", "VPN access", None, 10)
        assert {hit.group_id for hit in hits} == {own_group}


def next_provider_id(app: Any) -> str:
    with app.state.session_factory() as db:
        provider = db.scalar(select(LlmProviderConfig))
        assert provider is not None
        return cast(str, provider.id)
