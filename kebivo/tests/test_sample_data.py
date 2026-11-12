"""Removable sample rows and their outbound safety boundary."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import func, select

from kebivo.feedback.groups import check_integrity
from kebivo.models import (
    ApiToken,
    AuditEvent,
    Base,
    Delivery,
    Destination,
    EmbeddingBackfill,
    FeedbackEvent,
    FeedbackGroup,
    LlmProviderConfig,
    ProcessingJob,
    Project,
    QuestionEmbedding,
    RoutingRule,
    RoutingRuleDestination,
    Tag,
    now_utc,
)
from kebivo.retention import preview, run
from kebivo.tests.test_db import _alembic_config
from kebivo.tests.test_llm_providers import payload
from kebivo.tests.test_projects import make_client, write_headers
from kebivo.tests.test_semantic_dedup import fake
from kebivo.worker.runner import Worker
from scripts.fake_llm_server import FakeLLMServer

__all__ = ["fake"]

PATH = "/api/v1/admin/sample-data"
REFERENCING_TABLES = {
    "processing_jobs",
    "dedup_evaluations",
    "question_embeddings",
    "embedding_backfills",
    "routing_rules",
    "routing_rule_tags",
    "routing_rule_destinations",
    "routing_matches",
    "deliveries",
    "delivery_attempts",
    "projects",
    "tags",
    "feedback_events",
    "feedback_event_tags",
    "feedback_groups",
    "feedback_group_members",
    "feedback_group_notes",
    "feedback_group_history",
    "idempotency_records",
    "api_tokens",
}
TRACKED = {
    "projects",
    "destinations",
    "feedback_events",
    "feedback_groups",
    "routing_rules",
    "deliveries",
    "tags",
}


def test_sample_api_totals_status_conflicts_and_complete_removal(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "sample.db")
    headers = write_headers(client)
    assert client.get(PATH).json() == {
        "present": False,
        "has_real_events": False,
        "has_real_projects": False,
    }
    real = client.post(
        "/api/v1/projects", json={"name": "Real", "slug": "real"}, headers=headers
    ).json()
    assert client.get(PATH).json()["has_real_projects"] is True
    assert client.post(PATH).status_code == 403  # CSRF
    loaded = client.post(PATH, headers=headers)
    assert loaded.status_code == 201, loaded.text
    totals = loaded.json()
    assert totals == {"projects": 3, "groups": 14, "events": 43, "deliveries": 9}
    assert client.post(PATH, headers=headers).status_code == 409
    state = client.get(PATH).json()
    assert state == {"present": True, "has_real_events": False, "has_real_projects": True}
    groups = client.get("/api/v1/admin/groups?limit=100").json()["items"]
    summary = client.get("/api/v1/admin/groups/summary").json()
    deliveries = client.get("/api/v1/deliveries?limit=100").json()["items"]
    assert len(groups) == totals["groups"] == summary["total_groups"]
    top = groups[0]  # the inbox's default sort is last seen, newest first
    assert (top["project"]["name"], top["representative"]["question_excerpt"]) == (
        "Shop assistant",
        "Do you ship to Canada?",
    )
    assert top["occurrence_count"] == 7
    assert (
        sum(item["occurrence_count"] for item in groups)
        == totals["events"]
        == summary["total_events"]
    )
    assert len(deliveries) == totals["deliveries"]
    assert sum(item["status"] == "failed" for item in deliveries) == 1
    assert all(item["project"]["is_sample"] for item in groups)
    assert all(
        item["is_sample"]
        for item in client.get("/api/v1/destinations").json()["items"]
        if not item["is_system"]
    )
    sample_project = next(
        item for item in client.get("/api/v1/projects").json()["items"] if item["is_sample"]
    )
    assert (
        client.post(
            f"/api/v1/projects/{sample_project['id']}/tokens", json={"name": "No"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/projects/{sample_project['id']}/test-event", headers=headers
        ).status_code
        == 409
    )
    sample_destination = next(
        item for item in client.get("/api/v1/destinations").json()["items"] if item["is_sample"]
    )
    assert (
        client.patch(
            f"/api/v1/projects/{real['id']}",
            json={"fallback_destination_id": sample_destination["id"]},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/projects/{real['id']}/routing-rules",
            json={
                "name": "Unsafe",
                "event_types": ["knowledge_gap"],
                "priority": 1,
                "destination_ids": [sample_destination["id"]],
            },
            headers=headers,
        ).status_code
        == 422
    )

    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ProcessingJob)) == 0
        sample_groups = list(
            db.scalars(select(FeedbackGroup).join(Project).where(Project.is_sample.is_(True)))
        )
        last_seen = [group.last_seen_at for group in sample_groups]
        assert len(last_seen) == len(set(last_seen)) == totals["groups"]
        assert all(
            0.4 < (now_utc().replace(tzinfo=None) - seen).total_seconds() / 86400 < 14
            for seen in last_seen
        )
        ids = {
            "projects": set(db.scalars(select(Project.id).where(Project.is_sample.is_(True)))),
            "destinations": set(
                db.scalars(select(Destination.id).where(Destination.is_sample.is_(True)))
            ),
        }
        ids["feedback_events"] = set(
            db.scalars(
                select(FeedbackEvent.id).where(FeedbackEvent.project_id.in_(ids["projects"]))
            )
        )
        ids["feedback_groups"] = set(
            db.scalars(
                select(FeedbackGroup.id).where(FeedbackGroup.project_id.in_(ids["projects"]))
            )
        )
        ids["routing_rules"] = set(
            db.scalars(select(RoutingRule.id).where(RoutingRule.project_id.in_(ids["projects"])))
        )
        ids["deliveries"] = set(
            db.scalars(select(Delivery.id).where(Delivery.project_id.in_(ids["projects"])))
        )
        ids["tags"] = set(db.scalars(select(Tag.id).where(Tag.project_id.in_(ids["projects"]))))
        assert check_integrity(db) == []
    with app.state.session_factory() as db:
        destination = Destination(name="Real inbox", type="inbox")
        rule = RoutingRule(
            project_id=real["id"],
            name="Real rule",
            priority=1,
            event_types_json='["knowledge_gap"]',
            match_mode="any",
            match_behavior="continue",
        )
        token = ApiToken(
            project_id=real["id"], name="Real token", token_hash="a" * 64, token_prefix="real"
        )
        db.add_all((destination, rule, token))
        db.flush()
        event = FeedbackEvent(
            project_id=real["id"],
            api_token_id=token.id,
            submitted_via="rest",
            event_type="knowledge_gap",
            question="Real question",
            normalized_question_hash="b" * 64,
            processing_status="completed",
        )
        db.add_all((RoutingRuleDestination(rule_id=rule.id, destination_id=destination.id), event))
        db.commit()
        real_ids = (destination.id, rule.id, token.id, event.id)
    assert client.delete(PATH, headers=headers).status_code == 204
    assert client.delete(PATH, headers=headers).status_code == 204
    assert client.get(PATH).json() == {
        "present": False,
        "has_real_events": True,
        "has_real_projects": True,
    }
    assert client.get(f"/api/v1/projects/{real['id']}").status_code == 200
    assert client.get("/api/v1/admin/groups/summary").json()["total_events"] == 0
    assert client.post(f"/api/v1/projects/{real['id']}/archive", headers=headers).status_code == 200
    assert client.get(PATH).json()["has_real_projects"] is False
    with app.state.session_factory() as db:
        assert db.get(Destination, real_ids[0]) is not None
        assert db.get(RoutingRule, real_ids[1]) is not None
        assert db.get(ApiToken, real_ids[2]) is not None
        assert db.get(FeedbackEvent, real_ids[3]) is not None
        assert db.get(RoutingRuleDestination, (real_ids[1], real_ids[0])) is not None
        assert sorted(
            row.action
            for row in db.scalars(select(AuditEvent).where(AuditEvent.action.like("sample_data.%")))
        ) == ["sample_data.loaded", "sample_data.removed"]
        references = {
            table.name
            for table in Base.metadata.tables.values()
            if any(fk.column.table.name in TRACKED for fk in table.foreign_keys)
        }
        assert references == REFERENCING_TABLES
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                target = fk.column.table.name
                if target in ids and ids[target]:
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(table)
                            .where(fk.parent.in_(ids[target]))
                        )
                        == 0
                    ), (table.name, fk.parent.name)


def test_real_ungrouped_event_prevents_load(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "real-event.db")
    headers = write_headers(client)
    project = client.post(
        "/api/v1/projects", json={"name": "Real", "slug": "real"}, headers=headers
    ).json()
    with app.state.session_factory() as db:
        db.add(
            FeedbackEvent(
                project_id=project["id"],
                submitted_via="rest",
                event_type="knowledge_gap",
                question="Not grouped yet",
                normalized_question_hash="a" * 64,
                processing_status="pending",
            )
        )
        db.commit()
    assert client.get(PATH).json()["has_real_events"] is True
    assert client.post(PATH, headers=headers).status_code == 409


def test_sample_destination_never_sends_even_after_edit_and_retry(tmp_path: Path) -> None:
    received: list[bytes] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client, app = make_client(tmp_path / "outbound.db")
        headers = write_headers(client)
        assert client.post(PATH, headers=headers).status_code == 201
        destination = next(
            item
            for item in client.get("/api/v1/destinations").json()["items"]
            if item["name"] == "Sample API webhook"
        )
        edited = client.patch(
            f"/api/v1/destinations/{destination['id']}",
            json={
                "allow_private_network": True,
                "config": {"url": f"http://127.0.0.1:{server.server_port}/"},
            },
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert (
            client.post(
                f"/api/v1/destinations/{destination['id']}/test", headers=headers
            ).status_code
            == 409
        )
        with app.state.session_factory() as db:
            failed = db.scalar(
                select(Delivery).where(
                    Delivery.destination_id == destination["id"], Delivery.status == "failed"
                )
            )
            assert failed is not None
            delivery_id = failed.id
        retried = client.post(f"/api/v1/deliveries/{delivery_id}/retry", headers=headers)
        assert retried.status_code == 200, retried.text
        assert retried.json()["last_error_category"] == "sample_destination"
        assert received == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sample_rows_work_with_retention_and_group_check(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "retention.db")
    assert client.post(PATH, headers=write_headers(client)).status_code == 201
    with app.state.session_factory() as db:
        assert check_integrity(db) == []
    settings = replace(app.state.settings, feedback_retention_days=1)
    assert (
        preview(app.state.session_factory, settings, now_utc() + timedelta(days=1))["feedback"] > 0
    )
    assert run(app.state.session_factory, settings, now_utc() + timedelta(days=1))["feedback"] > 0
    with app.state.session_factory() as db:
        assert check_integrity(db) == []


def test_sample_project_never_reaches_embedding_provider(
    tmp_path: Path, fake: FakeLLMServer
) -> None:
    command.upgrade(_alembic_config(tmp_path / "sample.db"), "head")
    client, app = make_client(tmp_path / "sample.db")
    headers = write_headers(client)
    assert client.post(PATH, headers=headers).status_code == 201
    provider = client.post(
        "/api/v1/admin/llm-providers",
        json=payload(f"http://127.0.0.1:{fake.server_port}"),
        headers=headers,
    )
    assert provider.status_code == 201, provider.text
    semantic = {
        "method": "embeddings",
        "provider_id": provider.json()["id"],
        "embedding_model": "fake-64",
        "embedding_dimensions": 64,
    }
    dedup = {"mode": "semantic", "semantic": semantic, "privacy_acknowledged": True}
    sample = next(
        item for item in client.get("/api/v1/projects").json()["items"] if item["is_sample"]
    )
    judge = {"method": "judge", "provider_id": semantic["provider_id"]}
    for settings in (semantic, judge):
        refused = client.patch(
            f"/api/v1/projects/{sample['id']}",
            json={"settings": {"deduplication": dedup | {"semantic": settings}}},
            headers=headers,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "sample_project"
    preview = client.post(
        f"/api/v1/admin/projects/{sample['id']}/dedup/preview",
        json={"event_type": "knowledge_gap", "question": "Reset VPN"},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text

    # Rows written before this guard existed must still stay offline.
    worker = Worker(app.state.session_factory, app.state.settings)
    with app.state.session_factory() as db:
        project = db.get(Project, sample["id"])
        assert project is not None
        project.settings_json = json.dumps({"deduplication": dedup})
        db.add(
            EmbeddingBackfill(
                project_id=project.id,
                provider_id=semantic["provider_id"],
                embedding_revision=db.get(
                    LlmProviderConfig, semantic["provider_id"]
                ).embedding_revision,
                model="fake-64",
                remaining=100,
            )
        )
        event_id = db.scalar(
            select(FeedbackEvent.id).where(FeedbackEvent.project_id == project.id).limit(1)
        )
        assert event_id is not None
        job = ProcessingJob(
            event_id=event_id, status="processing", lease_owner=worker.boot_id
        )
        db.add(job)
        db.commit()
        job_id = job.id
    asyncio.run(worker._backfill())
    assert worker._prepare_semantic(job_id) is None
    asyncio.run(worker._process(job_id))
    assert fake.requests == []
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QuestionEmbedding)) == 0
