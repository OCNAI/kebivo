"""SQLite worker outcomes and lease recovery with a fake outbound adapter."""

import asyncio
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from kebivo.db import make_engine
from kebivo.destinations.base import REGISTRY, DeliveryResult, failure
from kebivo.feedback.groups import create_initial_group
from kebivo.models import (
    AuditEvent,
    Base,
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackEvent,
    FeedbackEventTag,
    MaintenanceRun,
    ProcessingJob,
    Project,
    RoutingMatch,
    RoutingRule,
    RoutingRuleDestination,
    RoutingRuleTag,
    Tag,
    now_utc,
)
from kebivo.security.secretbox import SecretBox
from kebivo.settings import Settings
from kebivo.tests.test_feedback_submission import post, setup
from kebivo.tests.test_projects import make_client, write_headers
from kebivo.worker.runner import Worker, send_destination


async def wait_until(predicate: Any, timeout: float = 6) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


def locked() -> OperationalError:
    return OperationalError("database is locked", {}, Exception("database is locked"))


def setup_worker(path: Path) -> tuple[Worker, sessionmaker[Session], str, str]:
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        project = Project(name="Project", slug="project")
        destination = Destination(name="Fake", type="inbox", config_json='{"timeout_seconds":2}')
        db.add_all([project, destination])
        db.flush()
        rule = RoutingRule(
            project_id=project.id,
            name="All",
            priority=1,
            enabled=True,
            event_types_json='["knowledge_gap"]',
            match_mode="any",
            match_behavior="continue",
        )
        db.add(rule)
        db.flush()
        db.add(RoutingRuleDestination(rule_id=rule.id, destination_id=destination.id))
        event = FeedbackEvent(
            project_id=project.id,
            submitted_via="rest",
            event_type="knowledge_gap",
            question="Question",
            normalized_question_hash="a" * 64,
        )
        db.add(event)
        db.flush()
        create_initial_group(db, event)
        job = ProcessingJob(event_id=event.id)
        db.add(job)
        db.commit()
        event_id, destination_id = event.id, destination.id
    settings = replace(
        Settings("test", "test", None, str(path)), delivery_retry_schedule_seconds=(1, 1, 1, 1, 1)
    )
    return Worker(factory, settings), factory, event_id, destination_id


def route(worker: Worker, factory: sessionmaker[Session], event_id: str) -> str:
    job_id = worker._claim("job", 1)[0]
    worker._process_sync(job_id)
    with factory() as db:
        delivery = db.scalar(select(Delivery).where(Delivery.event_id == event_id))
        assert delivery is not None
        return delivery.id


def due(factory: sessionmaker[Session], identifier: str) -> None:
    with factory() as db:
        row = db.get(Delivery, identifier)
        assert row is not None
        row.next_attempt_at = now_utc() - timedelta(seconds=1)
        db.commit()


def test_concurrent_workers_claim_each_delivery_once(tmp_path: Path) -> None:
    worker, factory, event_id, destination_id = setup_worker(tmp_path / "concurrent.db")
    with factory() as db:
        event = db.get(FeedbackEvent, event_id)
        assert event is not None
        for index in range(24):
            item = FeedbackEvent(
                project_id=event.project_id,
                submitted_via="rest",
                event_type="knowledge_gap",
                question=f"Question {index}",
                normalized_question_hash=f"{index:064x}",
            )
            db.add(item)
            db.flush()
            create_initial_group(db, item)
            db.add(
                Delivery(
                    event_id=item.id, project_id=item.project_id, destination_id=destination_id
                )
            )
        db.commit()
    other = Worker(factory, worker.settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(worker._claim, "delivery", 24)
        right = pool.submit(other._claim, "delivery", 24)
        claimed = left.result() + right.result()
    assert len(claimed) == 24
    assert len(set(claimed)) == 24
    with factory() as db:
        assert db.query(Delivery).filter(Delivery.status == "processing").count() == 24


def test_stop_waits_for_inflight_after_loop_failure(tmp_path: Path) -> None:
    worker, _, _, _ = setup_worker(tmp_path / "stop.db")
    completed = False

    async def failed_loop() -> None:
        raise RuntimeError("loop failed")

    async def in_flight() -> None:
        nonlocal completed
        await asyncio.sleep(0.01)
        completed = True

    async def run() -> None:
        worker.task = asyncio.create_task(failed_loop())
        worker.inflight.add(asyncio.create_task(in_flight()))
        await asyncio.sleep(0)
        await worker.stop()

    asyncio.run(run())
    assert completed


def test_simulator_matches_worker_for_seeded_cases(tmp_path: Path) -> None:
    rng = random.Random(85)
    client, app = make_client(tmp_path / "differential.db")
    factory = app.state.session_factory
    with factory() as db:
        project = Project(name="Differential", slug="differential")
        db.add(project)
        db.flush()
        destinations = [
            Destination(name=f"Target {i}", type="inbox", enabled=i != 2, config_json="{}")
            for i in range(3)
        ]
        tags = [Tag(project_id=project.id, name=f"Tag {i}", slug=f"tag{i}") for i in range(3)]
        db.add_all([*destinations, *tags])
        db.flush()
        project.fallback_destination_id = destinations[0].id
        for i in range(12):
            selected = rng.sample(tags, rng.randrange(3))
            rule = RoutingRule(
                project_id=project.id,
                name=f"Rule {i}",
                priority=rng.randrange(5),
                enabled=rng.choice([True, True, False]),
                event_types_json=json.dumps(
                    [rng.choice(["knowledge_gap", "knowledge_correction"])]
                ),
                match_mode=rng.choice(["any", "all"]),
                match_behavior=rng.choice(["continue", "stop"]),
            )
            db.add(rule)
            db.flush()
            db.add_all(RoutingRuleTag(rule_id=rule.id, tag_id=tag.id) for tag in selected)
            db.add_all(
                RoutingRuleDestination(rule_id=rule.id, destination_id=dest.id)
                for dest in rng.sample(destinations, rng.randrange(1, 4))
            )
        db.commit()
        project_id = project.id
        tag_ids = {tag.slug: tag.id for tag in tags}
    worker = Worker(factory, app.state.settings)
    for index in range(30):
        event_type = rng.choice(["knowledge_gap", "knowledge_correction"])
        selected_slugs = rng.sample(list(tag_ids), rng.randrange(4))
        simulated = client.post(
            f"/api/v1/projects/{project_id}/routing/simulate",
            json={"event_type": event_type, "tags": selected_slugs, "include_default_tags": False},
            headers=write_headers(client),
        )
        assert simulated.status_code == 200, simulated.text
        with factory() as db:
            event = FeedbackEvent(
                project_id=project_id,
                submitted_via="rest",
                event_type=event_type,
                question=f"Case {index}",
                correction="Corrected answer" if event_type == "knowledge_correction" else None,
                normalized_question_hash=f"{index:064x}",
            )
            db.add(event)
            db.flush()
            create_initial_group(db, event)
            db.add_all(
                FeedbackEventTag(event_id=event.id, tag_id=tag_ids[slug], from_submission=True)
                for slug in selected_slugs
            )
            db.add(ProcessingJob(event_id=event.id))
            db.commit()
            event_id = event.id
        claimed = worker._claim("job", 1)
        assert len(claimed) == 1
        worker._process_sync(claimed[0])
        with factory() as db:
            actual = db.scalars(select(RoutingMatch).where(RoutingMatch.event_id == event_id)).all()
        fields = ("destination_id", "via", "outcome", "rule_id")
        expected = [tuple(item[key] for key in fields) for item in simulated.json()["destinations"]]
        observed = [tuple(getattr(item, key) for key in fields) for item in actual]
        assert sorted(expected) == sorted(observed)


def test_loop_retries_claim_error_and_delivers(tmp_path: Path, monkeypatch: Any) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "loop-retry.db")
    worker.settings = replace(worker.settings, worker_poll_interval_seconds=1)
    original_claim = worker._claim
    calls = 0

    def fail_once(kind: Any, limit: int, semantic: bool | None = None) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise locked()
        return original_claim(kind, limit, semantic)

    monkeypatch.setattr(worker, "_claim", fail_once)

    async def run() -> None:
        await worker.start()
        try:

            def delivered() -> bool:
                with factory() as db:
                    row = db.scalar(select(Delivery).where(Delivery.event_id == event_id))
                    return row is not None and row.status == "delivered"

            await wait_until(delivered)
            assert worker.task is not None and not worker.task.done()
            assert calls > 1
        finally:
            await worker.stop()

    asyncio.run(run())


def test_finish_error_recovers_after_lease_expiry(tmp_path: Path, monkeypatch: Any) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "finish-retry.db")
    identifier = route(worker, factory, event_id)
    worker.settings = replace(worker.settings, worker_poll_interval_seconds=1)
    original_finish = worker._finish_attempt
    finish_calls = 0

    def fail_once(*args: Any) -> None:
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise locked()
        original_finish(*args)

    monkeypatch.setattr(worker, "_finish_attempt", fail_once)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await worker.start()
            try:
                await wait_until(lambda: finish_calls == 1 and not worker.inflight)
                with factory() as db:
                    row = db.get(Delivery, identifier)
                    assert row is not None and row.status == "processing"
                    row.lease_until = now_utc() - timedelta(seconds=1)
                    db.commit()
                worker.wake()

                def delivered() -> bool:
                    with factory() as db:
                        row = db.get(Delivery, identifier)
                        return row is not None and row.status == "delivered"

                await wait_until(delivered)
            finally:
                await worker.stop()
            assert not unhandled
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(run())
    with factory() as db:
        attempts = db.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == identifier)
            .order_by(DeliveryAttempt.attempt_number)
        ).all()
        assert [item.result for item in attempts] == ["interrupted", "success"]
        assert attempts[0].error_category == "interrupted"


def test_processing_is_idempotent_and_retries_then_succeeds(tmp_path: Path) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "worker.db")
    identifier = route(worker, factory, event_id)
    with factory() as db:
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.event_id == event_id))
        assert job is not None
        job.status = "pending"
        db.commit()
    worker._process_sync(worker._claim("job", 1)[0])
    with factory() as db:
        assert len(db.scalars(select(Delivery)).all()) == 1

    class Fake:
        def __init__(self) -> None:
            self.results = [
                failure("retryable", "network"),
                failure("retryable", "network"),
                DeliveryResult("success"),
            ]

        async def send(
            self,
            message: dict[str, Any],
            config: dict[str, Any],
            secrets: dict[str, Any],
            *,
            timeout: int,
        ) -> DeliveryResult:
            # An adapter can take SQLite's writer lock: the worker committed before calling it.
            with factory.kw["bind"].connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.rollback()
            return self.results.pop(0)

    original = REGISTRY["inbox"]
    REGISTRY["inbox"] = Fake()
    try:

        async def run() -> None:
            for index in range(3):
                due(factory, identifier)
                assert worker._claim("delivery", 1) == [identifier]
                await worker._deliver(identifier)
                with factory() as db:
                    row = db.get(Delivery, identifier)
                    assert row is not None
                    assert row.status == ("delivered" if index == 2 else "retry_scheduled")

        asyncio.run(run())
    finally:
        REGISTRY["inbox"] = original
    with factory() as db:
        attempts = db.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == identifier)
            .order_by(DeliveryAttempt.attempt_number)
        ).all()
        assert [item.result for item in attempts] == [
            "retryable_failure",
            "retryable_failure",
            "success",
        ]


def test_recovery_closes_open_attempt_and_resumes(tmp_path: Path) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "recovery.db")
    identifier = route(worker, factory, event_id)
    with factory() as db:
        row = db.get(Delivery, identifier)
        assert row is not None
        row.status = "processing"
        row.lease_owner = "abandoned-boot"
        row.lease_until = now_utc() + timedelta(minutes=1)
        row.attempt_count = 1
        db.add(DeliveryAttempt(delivery_id=identifier, attempt_number=1, trigger="auto"))
        db.commit()
    assert worker._recover()["deliveries"] == 1
    with factory() as db:
        row = db.get(Delivery, identifier)
        assert row is not None and row.status == "retry_scheduled"
        attempt = db.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == identifier)
        )
        assert attempt is not None and attempt.result == "interrupted"
        assert attempt.error_category == "interrupted"


def test_disabled_destination_is_held(tmp_path: Path) -> None:
    worker, factory, event_id, destination_id = setup_worker(tmp_path / "disabled.db")
    identifier = route(worker, factory, event_id)
    with factory() as db:
        destination = db.get(Destination, destination_id)
        assert destination is not None
        destination.enabled = False
        db.commit()
    assert worker._claim("delivery", 1) == []
    with factory() as db:
        destination = db.get(Destination, destination_id)
        assert destination is not None
        destination.enabled = True
        db.commit()
    assert worker._claim("delivery", 1) == [identifier]


def test_permanent_and_attempt_budget(tmp_path: Path) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "budget.db")
    identifier = route(worker, factory, event_id)

    class Fake:
        async def send(
            self,
            message: dict[str, Any],
            config: dict[str, Any],
            secrets: dict[str, Any],
            *,
            timeout: int,
        ) -> DeliveryResult:
            return failure("retryable", "network", retry_after=120)

    original = REGISTRY["inbox"]
    REGISTRY["inbox"] = Fake()
    try:

        async def run() -> None:
            for index in range(6):
                due(factory, identifier)
                assert worker._claim("delivery", 1) == [identifier]
                await worker._deliver(identifier)
                with factory() as db:
                    row = db.get(Delivery, identifier)
                    assert row is not None
                    assert row.status == ("failed" if index == 5 else "retry_scheduled")
                    if index == 0:
                        assert row.next_attempt_at is not None
                        assert row.next_attempt_at > now_utc().replace(tzinfo=None) + timedelta(
                            seconds=110
                        )

        asyncio.run(run())
    finally:
        REGISTRY["inbox"] = original
    with factory() as db:
        assert (
            len(
                db.scalars(
                    select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == identifier)
                ).all()
            )
            == 6
        )


def test_manual_retry_and_test_send_api(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "api.db")
    with app.state.session_factory() as db:
        project = Project(name="P", slug="p")
        destination = Destination(name="Inbox test", type="inbox", config_json="{}", enabled=False)
        db.add_all([project, destination])
        db.flush()
        event = FeedbackEvent(
            project_id=project.id,
            submitted_via="rest",
            event_type="knowledge_gap",
            question="Question",
            normalized_question_hash="b" * 64,
        )
        db.add(event)
        db.flush()
        create_initial_group(db, event)
        delivery = Delivery(
            event_id=event.id,
            project_id=project.id,
            destination_id=destination.id,
            status="failed",
            attempt_count=1,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                attempt_number=1,
                trigger="auto",
                completed_at=now_utc(),
                result="permanent_failure",
            )
        )
        db.commit()
        destination_id, delivery_id = destination.id, delivery.id
    headers = write_headers(client)
    assert (
        client.post(f"/api/v1/deliveries/{delivery_id}/retry", headers=headers).status_code == 409
    )
    test = client.post(f"/api/v1/destinations/{destination_id}/test", headers=headers)
    assert test.status_code == 200 and test.json()["outcome"] == "success"
    with app.state.session_factory() as db:
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "destination.tested"))
        assert audit is not None
        assert json.loads(audit.details_json) == {"outcome": "success", "category": None}
    with app.state.session_factory() as db:
        destination = db.get(Destination, destination_id)
        assert destination is not None
        destination.enabled = True
        db.commit()
    retried = client.post(f"/api/v1/deliveries/{delivery_id}/retry", headers=headers)
    assert retried.status_code == 200
    assert retried.json()["status"] == "delivered"
    assert [item["trigger"] for item in retried.json()["attempts"]] == ["auto", "manual"]
    assert (
        client.post(f"/api/v1/deliveries/{delivery_id}/retry", headers=headers).status_code == 409
    )
    anonymous = TestClient(app)
    for url in (
        "/api/v1/deliveries",
        f"/api/v1/deliveries/{delivery_id}",
        f"/api/v1/deliveries/{delivery_id}/retry",
        f"/api/v1/destinations/{destination_id}/test",
    ):
        method = anonymous.post if url.endswith(("/retry", "/test")) else anonymous.get
        assert method(url, headers={"Authorization": "Bearer project-token"}).status_code == 401


def test_webhook_sends_pinned_local_request_when_explicitly_allowed() -> None:
    received: list[tuple[str | None, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers.get("X-Delivery-ID"), json.loads(body)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = Settings("test", "test", None, ":memory:")
        secret = SecretBox.from_settings(settings).encrypt(
            "destination", {"url": f"http://127.0.0.1:{server.server_port}/hook"}
        )
        destination = {
            "id": "destination",
            "type": "webhook",
            "config": json.dumps(
                {
                    "auth": {"type": "none"},
                    "signing": False,
                    "custom_headers": [],
                    "timeout_seconds": 2,
                }
            ),
            "ciphertext": secret,
            "allow_private_network": True,
        }
        result = asyncio.run(
            send_destination(destination, {"delivery_id": "delivery", "test": True}, settings)
        )
        assert result.outcome == "success" and result.http_status == 204
        assert received == [("delivery", {"delivery_id": "delivery", "test": True})]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_shutdown_keeps_inflight_lease_for_restart(tmp_path: Path) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "shutdown.db")
    identifier = route(worker, factory, event_id)
    worker.settings = replace(worker.settings, worker_shutdown_grace_seconds=1)
    entered = asyncio.Event()

    class Slow:
        async def send(
            self,
            message: dict[str, Any],
            config: dict[str, Any],
            secrets: dict[str, Any],
            *,
            timeout: int,
        ) -> DeliveryResult:
            entered.set()
            await asyncio.sleep(30)
            return DeliveryResult("success")

    original = REGISTRY["inbox"]
    REGISTRY["inbox"] = Slow()
    try:

        async def run() -> None:
            await worker.start()
            worker.wake()
            await asyncio.wait_for(entered.wait(), timeout=3)
            await worker.stop()

        asyncio.run(run())
    finally:
        REGISTRY["inbox"] = original
    with factory() as db:
        row = db.get(Delivery, identifier)
        assert row is not None and row.status == "processing"
        assert row.lease_owner == worker.boot_id and row.lease_until is not None
        attempt = db.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == identifier)
        )
        assert attempt is not None and attempt.completed_at is None


def test_rest_acceptance_routes_to_inbox_and_exposes_delivery_api(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    with app.state.session_factory() as db:
        destination = Destination(name="Inbox route", type="inbox", config_json="{}")
        db.add(destination)
        db.flush()
        project_row = db.get(Project, project["id"])
        assert project_row is not None
        project_row.fallback_destination_id = destination.id
        db.commit()
    response = post(client, token, {"event_type": "knowledge_gap", "question": "Where is it?"})
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    worker: Worker = app.state.worker
    assert worker is not None
    worker._process_sync(worker._claim("job", 1)[0])
    delivery_id = worker._claim("delivery", 1)[0]
    asyncio.run(worker._deliver(delivery_id))
    detail = client.get(f"/api/v1/admin/feedback/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["routing"]["processing_status"] == "completed"
    assert detail.json()["routing"]["deliveries"][0]["status"] == "delivered"
    listed = client.get("/api/v1/deliveries", params={"event_id": event_id})
    assert listed.status_code == 200 and listed.json()["items"][0]["id"] == delivery_id
    assert (
        client.get(f"/api/v1/deliveries/{delivery_id}").json()["attempts"][0]["result"] == "success"
    )


def insert_expired_events(factory: sessionmaker[Session], project_id: str, count: int) -> None:
    old = "2020-01-01T00:00:00"
    events, groups, members = [], [], []
    for number in range(count):
        event_id, group_id = str(uuid4()), str(uuid4())
        events.append(
            {"id": event_id, "project_id": project_id, "q": f"expired-{number}", "t": old}
        )
        groups.append({"id": group_id, "project_id": project_id, "event_id": event_id, "t": old})
        members.append({"event_id": event_id, "group_id": group_id, "t": old})
    with factory() as db:
        db.execute(
            text(
                "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,"
                "normalized_question_hash,created_at,updated_at) "
                "VALUES (:id,:project_id,'rest','knowledge_gap',:q,:q,:t,:t)"
            ),
            events,
        )
        db.execute(
            text(
                "INSERT INTO feedback_groups(id,project_id,event_type,representative_event_id,"
                "status,occurrence_count,first_seen_at,last_seen_at,status_changed_at,"
                "created_at,updated_at) VALUES (:id,:project_id,'knowledge_gap',:event_id,"
                "'open',1,:t,:t,:t,:t,:t)"
            ),
            groups,
        )
        db.execute(
            text(
                "INSERT INTO feedback_group_members(event_id,group_id,match_method,added_at) "
                "VALUES (:event_id,:group_id,'initial',:t)"
            ),
            members,
        )
        db.commit()


def test_delivery_completes_while_retention_runs_and_stop_cancels_it(tmp_path: Path) -> None:
    worker, factory, event_id, _ = setup_worker(tmp_path / "retention-lane.db")
    identifier = route(worker, factory, event_id)
    due(factory, identifier)
    with factory() as db:
        event = db.get(FeedbackEvent, event_id)
        assert event is not None
        project_id = event.project_id
    # 3,000 events at 25 per batch with a 0.1 s gap is a run of well over 10 s.
    insert_expired_events(factory, project_id, 3_000)
    worker.retention_check_at = 0

    def delivered() -> bool:
        with factory() as db:
            row = db.get(Delivery, identifier)
            return row is not None and row.status == "delivered"

    def retention_running() -> bool:
        with factory() as db:
            row = db.scalar(select(MaintenanceRun))
            return row is not None and row.status == "running"

    async def run() -> None:
        await worker.start()
        try:
            await wait_until(retention_running)
            await wait_until(delivered)
            assert worker.retention_task is not None and not worker.retention_task.done()
            assert retention_running()
        finally:
            started = time.monotonic()
            await worker.stop()
            assert time.monotonic() - started < 5
        assert worker.retention_task is not None and worker.retention_task.done()

    asyncio.run(run())
    with factory() as db:
        row = db.scalar(select(MaintenanceRun))
        assert row is not None
        assert row.status == "failed" and row.error_category == "RetentionCancelled"
        remaining = db.scalar(select(func.count()).select_from(FeedbackEvent))
        assert remaining is not None and 0 < remaining < 3_001
