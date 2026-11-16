"""Weekly digest schedule, content, delivery and API."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kebivo import digest
from kebivo.db import make_engine
from kebivo.destinations import base
from kebivo.destinations.base import REGISTRY, DeliveryResult
from kebivo.destinations.smtp import _mail
from kebivo.feedback.groups import associate, create_initial_group
from kebivo.models import (
    Base,
    Delivery,
    Destination,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupHistory,
    ProcessingJob,
    Project,
    RoutingMatch,
    RoutingRule,
    RoutingRuleDestination,
    now_utc,
)
from kebivo.settings import Settings
from kebivo.tests.test_projects import make_client, write_headers
from kebivo.worker.runner import Worker


def test_latest_slot_is_the_most_recent_weekday_hour() -> None:
    wednesday = datetime(2026, 11, 18, 12, tzinfo=UTC)
    assert digest.latest_slot(wednesday, 0, 9) == datetime(2026, 11, 16, 9)
    assert digest.latest_slot(wednesday, 2, 12) == datetime(2026, 11, 18, 12)
    assert digest.latest_slot(wednesday, 2, 13) == datetime(2026, 11, 11, 13)
    assert digest.period_start(datetime(2026, 11, 16, 9)) == datetime(2026, 11, 9, 9)


def test_slot_follows_server_time_zone_across_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("TZ", "Europe/Oslo")
    time.tzset()
    try:
        # Monday 09:30 in Oslo, the day after clocks went back.
        monday = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
        slot = digest.latest_slot(monday, 0, 9)
        assert slot == datetime(2026, 10, 26, 8)
        assert digest.period_start(slot) == datetime(2026, 10, 19, 7)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


class World:
    def __init__(self, path: Path) -> None:
        engine = make_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        self.factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
        self.settings = replace(
            Settings("test", "test", None, str(path)), public_url="https://kebivo.example"
        )
        self.slot = digest.latest_slot(now_utc(), 0, 9)
        with self.factory() as db:
            self.destination = Destination(
                name="Digest",
                type="inbox",
                config_json='{"timeout_seconds":2}',
                delivery_mode="weekly_digest",
                digest_weekday=0,
                digest_hour=9,
                digest_checked_through=self.slot - timedelta(days=7),
            )
            db.add(self.destination)
            db.commit()
        self.projects: dict[str, str] = {}

    def project(self, name: str, *, sample: bool = False) -> str:
        with self.factory() as db:
            project = Project(name=name, slug=name.lower(), is_sample=sample)
            db.add(project)
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
            db.add(RoutingRuleDestination(rule_id=rule.id, destination_id=self.destination.id))
            db.commit()
            return project.id

    def group(self, project_id: str, question: str, count: int, age: timedelta) -> str:
        """Add count routed occurrences to one new group, created age before the slot."""
        with self.factory() as db:
            group: FeedbackGroup | None = None
            for _ in range(count):
                event = FeedbackEvent(
                    project_id=project_id,
                    submitted_via="rest",
                    event_type="knowledge_gap",
                    question=question,
                    normalized_question_hash="a" * 64,
                    created_at=self.slot.replace(tzinfo=UTC) - age,
                )
                db.add(event)
                db.flush()
                if group is None:
                    group = create_initial_group(db, event)
                else:
                    associate(db, event, group, "exact")
                db.add(ProcessingJob(event_id=event.id))
            db.commit()
            assert group is not None
            return group.id

    def route_all(self) -> None:
        worker = Worker(self.factory, self.settings)
        while jobs := worker._claim("job", 10):
            for job in jobs:
                worker._process_sync(job)

    def status(self, group_id: str, previous: str, status: str, age: timedelta) -> None:
        with self.factory() as db:
            group = db.get(FeedbackGroup, group_id)
            assert group is not None
            group.status = status
            db.add(
                FeedbackGroupHistory(
                    group_id=group_id,
                    action="status_changed",
                    actor_type="admin",
                    from_status=previous,
                    to_status=status,
                    created_at=self.slot.replace(tzinfo=UTC) - age,
                )
            )
            db.commit()

    def digests(self) -> list[Delivery]:
        with self.factory() as db:
            return list(db.scalars(select(Delivery).where(Delivery.event_id.is_(None))))


def test_routing_queues_for_digest_without_event_delivery(tmp_path: Path) -> None:
    world = World(tmp_path / "digest.db")
    project = world.project("Bot")
    world.group(project, "Where is the refund form?", 2, timedelta(hours=1))
    world.route_all()
    with world.factory() as db:
        assert db.scalars(select(Delivery)).all() == []
        outcomes = db.scalars(select(RoutingMatch.outcome)).all()
    assert outcomes == ["digest_queued", "digest_queued"]


def test_digest_lists_top_open_groups_and_status_counts(tmp_path: Path) -> None:
    world = World(tmp_path / "digest.db")
    project = world.project("Bot")
    hour = timedelta(hours=1)
    ids = [world.group(project, f"Question {n}", n, hour * n) for n in range(1, 8)]
    old = world.group(project, "Last month", 9, timedelta(days=30))
    world.route_all()
    world.status(ids[6], "open", "resolved", hour)  # resolved this week: not in the list
    world.status(old, "open", "resolved", timedelta(days=20))
    world.status(old, "resolved", "open", hour)  # reopened this week
    sample = world.project("Sample", sample=True)
    world.group(sample, "Sample question", 20, hour)
    world.route_all()
    with world.factory() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        assert digest.queue_due(db) == 1
        db.commit()
    [row] = world.digests()
    assert row.project_id == project
    with world.factory() as db:
        message = digest.payload(db, row, world.settings.public_url)
    assert message["event_type"] == "weekly_digest"
    assert [item["question"] for item in message["top_groups"]] == [
        f"Question {n}" for n in (6, 5, 4, 3, 2)
    ]
    assert message["top_groups"][0] == {
        "group_id": ids[5],
        "question": "Question 6",
        "occurrences": 6,
        "last_seen_at": (world.slot - hour * 6).replace(tzinfo=UTC).isoformat(),
        "url": f"https://kebivo.example/inbox/groups/{ids[5]}",
    }
    assert message["open_group_count"] == 6
    assert (message["resolved_count"], message["reopened_count"]) == (1, 1)
    assert message["period"] == {
        "start": digest.period_start(world.slot).replace(tzinfo=UTC).isoformat(),
        "end": world.slot.replace(tzinfo=UTC).isoformat(),
    }
    schema = json.loads(
        (Path(__file__).parents[2] / "docs/webhook-digest-payload.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(message)


def test_empty_week_and_new_schedule_send_nothing(tmp_path: Path) -> None:
    world = World(tmp_path / "digest.db")
    project = world.project("Bot")
    world.group(project, "Old", 3, timedelta(days=10))
    world.route_all()
    with world.factory() as db:
        assert digest.queue_due(db) == 0
        destination = db.get(Destination, world.destination.id)
        assert destination is not None
        assert destination.digest_checked_through == world.slot
        # A fresh schedule starts at the latest slot instead of sending at once.
        destination.digest_checked_through = None
        world.group(project, "New", 1, timedelta(hours=1))
        db.commit()
    world.route_all()
    with world.factory() as db:
        assert digest.queue_due(db) == 0
        db.commit()
    assert world.digests() == []


def test_missed_slot_sends_once_on_start_and_never_twice(tmp_path: Path) -> None:
    world = World(tmp_path / "digest.db")
    project = world.project("Bot")
    world.group(project, "Missed while down", 2, timedelta(hours=2))
    world.route_all()
    sent: list[dict[str, Any]] = []

    class Capture:
        async def send(
            self, message: dict[str, Any], config: dict[str, Any], secrets: dict[str, Any], **_: Any
        ) -> DeliveryResult:
            sent.append(message)
            return DeliveryResult("success")

    original = REGISTRY["inbox"]
    REGISTRY["inbox"] = Capture()

    async def boot() -> None:
        worker = Worker(world.factory, world.settings)
        await worker.start()
        for _ in range(300):
            rows = world.digests()
            if rows and rows[0].status == "delivered":
                break
            await asyncio.sleep(0.02)
        await worker.stop()

    try:
        asyncio.run(boot())  # the container comes back up after the slot passed
        asyncio.run(boot())  # and restarts again within the same week
        with world.factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert digest.queue_due(db) == 0
            db.commit()
    finally:
        REGISTRY["inbox"] = original
    [row] = world.digests()
    assert row.status == "delivered" and row.attempt_count == 1
    assert [item["top_groups"][0]["question"] for item in sent] == ["Missed while down"]
    assert sent[0]["delivery_id"] == row.id


def test_slack_and_email_render_digest(monkeypatch: Any) -> None:
    message = digest.test_digest_payload()
    message["top_groups"][0]["url"] = "https://kebivo.example/inbox/groups/g"
    posted: list[dict[str, Any]] = []

    async def post(url: str, headers: dict[str, str], body: bytes, **_: Any) -> tuple[int, None]:
        posted.append(json.loads(body))
        return 200, None

    monkeypatch.setattr(base, "post_pinned", post)
    result = asyncio.run(
        REGISTRY["slack"].send(
            message, {}, {"url": "https://hooks.slack.com/services/T/B/S"}, timeout=5
        )
    )
    assert result.outcome == "success"
    blocks = posted[0]["blocks"]
    assert blocks[0]["text"]["text"] == "Kebivo weekly digest: Test project"
    assert blocks[2]["text"] == {
        "type": "plain_text",
        "text": f"3× Test weekly digest delivery (last seen {message['period']['end'][:10]})",
    }
    assert blocks[2]["accessory"]["url"] == "https://kebivo.example/inbox/groups/g"
    mail = _mail(message, {"from": "bot@example.com", "to": ["team@example.com"]})
    assert mail["Subject"] == "[TEST] [Weekly digest] Test project — 1 open groups"
    text = mail.get_body(("plain",))
    assert text is not None
    assert "3× Test weekly digest delivery" in text.get_content()
    assert "https://kebivo.example/inbox/groups/g" in text.get_content()


def test_destination_api_schedules_digest(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path / "api.db")
    headers = write_headers(client)
    body = {
        "name": "Weekly",
        "type": "webhook",
        "delivery_mode": "weekly_digest",
        "digest_weekday": 4,
        "digest_hour": 16,
        "config": {"url": "https://example.com/hook"},
    }
    created = client.post("/api/v1/destinations", json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert (created.json()["digest_weekday"], created.json()["digest_hour"]) == (4, 16)
    identifier = created.json()["id"]
    for patch, field in (
        ({"digest_hour": 24}, "digest_hour"),
        ({"digest_weekday": 7}, "digest_weekday"),
    ):
        response = client.patch(f"/api/v1/destinations/{identifier}", json=patch, headers=headers)
        assert response.status_code == 422
        assert response.json()["error"]["details"]["fields"][0]["field"] == field
    changed = client.patch(
        f"/api/v1/destinations/{identifier}",
        json={"delivery_mode": "new_groups_only"},
        headers=headers,
    )
    assert changed.json()["digest_weekday"] is None
    inbox = client.post(
        "/api/v1/destinations",
        json={"name": "Inbox", "type": "inbox", "delivery_mode": "weekly_digest", "config": {}},
        headers=headers,
    )
    assert inbox.status_code == 422


@pytest.mark.parametrize("weekday", [0, 6])
def test_default_hour_is_nine(tmp_path: Path, weekday: int) -> None:
    client, _ = make_client(tmp_path / "api.db")
    headers = write_headers(client)
    created = client.post(
        "/api/v1/destinations",
        json={
            "name": "Weekly",
            "type": "webhook",
            "delivery_mode": "weekly_digest",
            "digest_weekday": weekday,
            "config": {"url": "https://example.com/hook"},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert (created.json()["digest_weekday"], created.json()["digest_hour"]) == (weekday, 9)


def test_migration_keeps_rows_and_downgrade_needs_no_digest_mode(tmp_path: Path) -> None:
    import sqlite3

    from alembic import command

    from kebivo.tests.test_db import _alembic_config

    path = tmp_path / "migration.db"
    config = _alembic_config(path)
    command.upgrade(config, "0018_star_prompt_dismissal")
    now = "2026-11-16 09:00:00"
    with sqlite3.connect(path) as db:
        db.executescript(f"""
            INSERT INTO projects (id,name,slug,created_at,updated_at)
                VALUES ('p','Bot','bot','{now}','{now}');
            INSERT INTO destinations (id,name,type,created_at,updated_at)
                VALUES ('d','Hook','webhook','{now}','{now}');
            INSERT INTO feedback_events (id,project_id,submitted_via,event_type,question,
                normalized_question_hash,created_at,updated_at)
                VALUES ('e','p','rest','knowledge_gap','Q','h','{now}','{now}');
            INSERT INTO routing_matches (id,event_id,via,destination_id,outcome,created_at)
                VALUES ('m','e','rule','d','delivery_created','{now}');
            INSERT INTO deliveries (id,event_id,project_id,destination_id,created_at,updated_at)
                VALUES ('x','e','p','d','{now}','{now}');
            INSERT INTO delivery_attempts (id,delivery_id,attempt_number,"trigger",started_at)
                VALUES ('a','x',1,'auto','{now}');
        """)
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM delivery_attempts").fetchone() == (1,)
        assert db.execute("SELECT event_id FROM deliveries").fetchall() == [("e",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.executescript(f"""
            UPDATE destinations SET delivery_mode='weekly_digest', digest_weekday=0,
                digest_hour=9 WHERE id='d';
            INSERT INTO deliveries (id,event_id,project_id,destination_id,digest_period_start,
                digest_period_end,created_at,updated_at)
                VALUES ('y',NULL,'p','d','2026-11-09 09:00:00','{now}','{now}','{now}');
        """)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO deliveries (id,event_id,project_id,destination_id,"
                "created_at,updated_at) VALUES ('z',NULL,'p','d',?,?)",
                (now, now),
            )
    with pytest.raises(RuntimeError, match="weekly digest"):
        command.downgrade(config, "0018_star_prompt_dismissal")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE destinations SET delivery_mode='new_groups_only'")
    command.downgrade(config, "0018_star_prompt_dismissal")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM deliveries").fetchall() == [("x",)]
        assert db.execute("SELECT count(*) FROM delivery_attempts").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_webhook_digest_is_signed_and_matches_schema(monkeypatch: Any) -> None:
    import base64
    import hashlib
    import hmac

    received: list[tuple[dict[str, str], bytes]] = []

    async def post(url: str, headers: dict[str, str], body: bytes, **_: Any) -> tuple[int, None]:
        received.append((headers, body))
        return 202, None

    monkeypatch.setattr(base, "post_pinned", post)
    key = b"k" * 32
    message = digest.test_digest_payload()
    result = asyncio.run(
        REGISTRY["webhook"].send(
            message,
            {"signing": True},
            {
                "url": "https://example.com/hook",
                "signing_secret": "whsec_" + base64.b64encode(key).decode(),
            },
            timeout=5,
        )
    )
    assert result.outcome == "success"
    headers, body = received[0]
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == "v1," + expected
    assert headers["webhook-id"] == message["delivery_id"]
    schema = json.loads(
        (Path(__file__).parents[2] / "docs/webhook-digest-payload.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(json.loads(body))
