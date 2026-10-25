"""Phase 6 exact matching and delivery decisions."""

import random
from pathlib import Path
from typing import Literal, cast

import pytest
from alembic import command
from sqlalchemy import select

from kebivo.dedup.normalize import normalize_v2, question_hash
from kebivo.feedback.groups import check_integrity
from kebivo.models import (
    Delivery,
    Destination,
    FeedbackEvent,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    ProcessingJob,
    RoutingMatch,
    RoutingRule,
    RoutingRuleDestination,
    now_utc,
)
from kebivo.routing.engine import DestinationInput, evaluate
from kebivo.tests.test_feedback_groups_foundation import config
from kebivo.tests.test_feedback_submission import post, setup
from kebivo.tests.test_projects import write_headers
from kebivo.tests.test_tokens import create_project, issue_token
from kebivo.worker.runner import Worker


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("ＡＢＣ", "abc"),
        ("Straße", "STRASSE"),
        ("‘yes’ “now”", "'yes' \"now\""),
        ("a–b", "a-b"),
        ("a−b", "a-b"),
        ("a \n b", "a b"),
        ("Why now？！", "why now"),
        ("Why now ? ! .", "why now"),
    ],
)
def test_normalizer_equivalence(left: str, right: str) -> None:
    assert normalize_v2(left) == normalize_v2(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("VPN", "VPS"),
        ("a,b", "ab"),
        ("the answer", "answer"),
        ("reset", "resets"),
    ],
)
def test_normalizer_does_not_overmatch(left: str, right: str) -> None:
    assert normalize_v2(left) != normalize_v2(right)


def test_exact_association_reopen_and_dismissed(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    path = f"/api/v1/projects/{project['id']}"
    assert (
        client.patch(
            path,
            json={
                "settings": {"corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}
            },
            headers=write_headers(client),
        ).status_code
        == 200
    )
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How do I reset my VPN?"}
    ).json()["event_id"]
    with app.state.session_factory() as db:
        original = db.get(FeedbackEvent, first)
        assert original is not None
        snapshot = (original.question, original.metadata_json, original.created_at)
        group_id = db.get(FeedbackGroupMember, first).group_id
        group = db.get(FeedbackGroup, group_id)
        assert group is not None
        group.status = "resolved"
        group.resolved_at = group.status_changed_at
        db.commit()
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "  how do I reset my VPN  "}
    ).json()["event_id"]
    with app.state.session_factory() as db:
        group = db.get(FeedbackGroup, group_id)
        member = db.get(FeedbackGroupMember, second)
        original = db.get(FeedbackEvent, first)
        assert group is not None and member is not None and original is not None
        assert (group.status, group.occurrence_count, group.resolved_at) == ("open", 2, None)
        assert (
            member.group_id == group_id
            and member.match_method == "exact"
            and member.triggered_reopen
        )
        assert (original.question, original.metadata_json, original.created_at) == snapshot
        assert [
            row.action
            for row in db.scalars(
                select(FeedbackGroupHistory).where(FeedbackGroupHistory.group_id == group_id)
            )
        ][-2:] == ["occurrence_added", "status_changed"]
        group.status = "dismissed"
        db.commit()
    third = post(
        client, token, {"event_type": "knowledge_gap", "question": "How do I reset my VPN!"}
    ).json()["event_id"]
    with app.state.session_factory() as db:
        assert db.get(FeedbackGroup, group_id).status == "dismissed"
        assert not db.get(FeedbackGroupMember, third).triggered_reopen
        assert check_integrity(db) == []
    items = client.get(f"/api/v1/admin/groups/{group_id}/events").json()["items"]
    assert {item["match"]["method"] for item in items} == {"initial", "exact"}


def test_project_and_type_boundaries_and_settings_validation(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    path = f"/api/v1/projects/{project['id']}"
    headers = write_headers(client)
    for invalid in ({"mode": "semantic"}, {"mode": "exact", "extra": True}, {"enabled": "yes"}):
        assert (
            client.patch(
                path, json={"settings": {"deduplication": invalid}}, headers=headers
            ).status_code
            == 422
        )
    assert client.patch(
        path,
        json={"settings": {"corrections": {"enabled": True}, "deduplication": {"enabled": True}}},
        headers=headers,
    ).json()["settings"]["deduplication"] == {"mode": "exact"}
    other = create_project(client, "second", corrections=True)
    assert (
        client.patch(
            f"/api/v1/projects/{other['id']}",
            json={
                "settings": {"corrections": {"enabled": True}, "deduplication": {"mode": "exact"}}
            },
            headers=headers,
        ).status_code
        == 200
    )
    other_token = issue_token(
        client, other["id"], "second", ["feedback:gap", "feedback:correction"]
    ).json()["token"]
    question = "Same question?"
    ids = [
        post(client, token, {"event_type": "knowledge_gap", "question": question}).json()[
            "event_id"
        ],
        post(
            client,
            token,
            {"event_type": "knowledge_correction", "question": question, "correction": "Fix"},
        ).json()["event_id"],
        post(client, other_token, {"event_type": "knowledge_gap", "question": question}).json()[
            "event_id"
        ],
    ]
    with app.state.session_factory() as db:
        assert len({db.get(FeedbackGroupMember, event_id).group_id for event_id in ids}) == 3
        assert check_integrity(db) == []


def test_seeded_exact_ingest_sequence_keeps_group_integrity(tmp_path: Path) -> None:
    client, app, project, token = setup(tmp_path)
    headers = write_headers(client)
    assert (
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"settings": {"deduplication": {"mode": "exact"}}},
            headers=headers,
        ).status_code
        == 200
    )
    rng = random.Random(115)
    for step in range(30):
        topic = rng.choice(("VPN", "VPS", "Email", "Billing"))
        suffix = rng.choice(("?", " !", ""))
        response = post(
            client, token, {"event_type": "knowledge_gap", "question": f"Where is {topic}{suffix}"}
        )
        assert response.status_code == 202, step
        with app.state.session_factory() as db:
            assert check_integrity(db) == [], step
            assert db.get(FeedbackGroupMember, response.json()["event_id"]) is not None
    with app.state.session_factory() as db:
        assert len(db.scalars(select(FeedbackGroup)).all()) == 4


@pytest.mark.parametrize(
    ("mode", "initial", "reopened", "previous", "expected"),
    [
        ("new_groups_only", True, False, True, "delivery_created"),
        ("new_groups_only", False, False, True, "suppressed_duplicate"),
        ("new_groups_only", False, False, False, "delivery_created"),
        ("new_groups_only", False, True, True, "delivery_created"),
        ("every_occurrence", False, False, True, "delivery_created"),
    ],
)
def test_delivery_modes(
    mode: str, initial: bool, reopened: bool, previous: bool, expected: str
) -> None:
    result = evaluate(
        "knowledge_gap",
        set(),
        [],
        [DestinationInput("d", True, cast(Literal["new_groups_only", "every_occurrence"], mode))],
        "d",
        initial=initial,
        triggered_reopen=reopened,
        previously_delivered=frozenset({"d"}) if previous else frozenset(),
    )
    assert result.destinations[0].outcome == expected


@pytest.mark.parametrize("mode", ["new_groups_only", "every_occurrence"])
@pytest.mark.parametrize("order", ["first", "second", "retry", "reopen"])
def test_worker_routes_group_once_regardless_of_job_order(
    tmp_path: Path, mode: str, order: str
) -> None:
    client, app, project, token = setup(tmp_path)
    factory = app.state.session_factory
    response = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"settings": {"deduplication": {"mode": "exact"}}},
        headers=write_headers(client),
    )
    assert response.status_code == 200
    with factory() as db:
        destination = Destination(name="Inbox", type="inbox", delivery_mode=mode, config_json="{}")
        rule = RoutingRule(
            project_id=project["id"],
            name="Gaps",
            priority=1,
            enabled=True,
            event_types_json='["knowledge_gap"]',
            match_mode="any",
            match_behavior="continue",
        )
        db.add_all([destination, rule])
        db.flush()
        db.add(RoutingRuleDestination(rule_id=rule.id, destination_id=destination.id))
        db.commit()
        destination_id = destination.id
    first = post(
        client, token, {"event_type": "knowledge_gap", "question": "How do I reset my VPN?"}
    ).json()["event_id"]
    if order == "reopen":
        with factory() as db:
            member = db.get(FeedbackGroupMember, first)
            assert member is not None
            group = db.get(FeedbackGroup, member.group_id)
            assert group is not None
            group.status = "resolved"
            group.resolved_at = now_utc()
            db.commit()
    second = post(
        client, token, {"event_type": "knowledge_gap", "question": "how do i reset my vpn"}
    ).json()["event_id"]
    with factory() as db:
        first_member = db.get(FeedbackGroupMember, first)
        second_member = db.get(FeedbackGroupMember, second)
        assert first_member is not None and second_member is not None
        assert first_member.group_id == second_member.group_id
        assert (first_member.match_method, second_member.match_method) == ("initial", "exact")
        assert second_member.triggered_reopen is (order == "reopen")
        group_id = first_member.group_id
        jobs = {
            row.event_id: row.id
            for row in db.scalars(
                select(ProcessingJob).where(ProcessingJob.event_id.in_((first, second)))
            )
        }
    worker = Worker(factory, app.state.settings)
    assert set(worker._claim("job", 2)) == set(jobs.values())
    if order == "retry":
        worker._process_failed(jobs[first])
    if order != "first":
        if order != "reopen":
            simulated = client.post(
                f"/api/v1/projects/{project['id']}/routing/simulate",
                json={"event_type": "knowledge_gap", "group_id": group_id},
                headers=write_headers(client),
            )
            assert simulated.status_code == 200
            assert simulated.json()["destinations"][0]["outcome"] == (
                "suppressed_duplicate" if mode == "new_groups_only" else "delivery_created"
            )
        worker._process_sync(jobs[second])
    if order == "retry":
        with factory() as db:
            job = db.get(ProcessingJob, jobs[first])
            assert job is not None and job.status == "pending" and job.attempt_count == 1
            job.next_attempt_at = now_utc()
            db.commit()
        assert worker._claim("job", 1) == [jobs[first]]
    worker._process_sync(jobs[first])
    if order == "first":
        worker._process_sync(jobs[second])
    with factory() as db:
        deliveries = db.scalars(
            select(Delivery).where(
                Delivery.event_id.in_((first, second)), Delivery.destination_id == destination_id
            )
        ).all()
        assert len(deliveries) == (1 if mode == "new_groups_only" and order != "reopen" else 2)
        if mode == "new_groups_only" and order != "reopen":
            assert deliveries[0].event_id == first
        outcomes = {
            row.event_id: row.outcome
            for row in db.scalars(
                select(RoutingMatch).where(RoutingMatch.event_id.in_((first, second)))
            )
        }
        assert outcomes == {
            first: "delivery_created",
            second: (
                "suppressed_duplicate"
                if mode == "new_groups_only" and order != "reopen"
                else "delivery_created"
            ),
        }


def test_migration_rehashes_populated_data_and_downgrades(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    command.upgrade(config(path), "0008_feedback_management")
    from kebivo.db import make_engine

    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO projects(id,name,slug,settings_json,is_active,created_at,updated_at) "
            "VALUES ('p','P','p','{\"deduplication\":{\"enabled\":true}}',1,"
            "'2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_events(id,project_id,submitted_via,event_type,question,"
            "normalized_question_hash,question_hash_version,metadata_json,processing_status,"
            "created_at,updated_at) VALUES ('e','p','rest','knowledge_gap','VPN?','old',1,"
            "'{}','pending','2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_groups(id,project_id,event_type,representative_event_id,"
            "status,status_changed_at,occurrence_count,first_seen_at,last_seen_at,"
            "created_at,updated_at) VALUES ('g','p','knowledge_gap','e','open',"
            "'2026-01-01',1,'2026-01-01','2026-01-01','2026-01-01','2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO feedback_group_members(event_id,group_id,match_method,added_at) "
            "VALUES ('e','g','initial','2026-01-01')"
        )
    engine.dispose()
    command.upgrade(config(path), "0009_exact_dedup")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT normalized_question_hash,question_hash_version FROM feedback_events "
            "WHERE id='e'"
        ).one() == (question_hash("p", "knowledge_gap", "VPN?"), 2)
        assert (
            '"mode":"exact"'
            in conn.exec_driver_sql("SELECT settings_json FROM projects WHERE id='p'").scalar_one()
        )
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        assert check_integrity(db) == []
    engine.dispose()
    command.downgrade(config(path), "0008_feedback_management")
    engine = make_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT question_hash_version FROM feedback_events WHERE id='e'"
            ).scalar_one()
            == 1
        )
        assert (
            '"enabled":true'
            in conn.exec_driver_sql("SELECT settings_json FROM projects WHERE id='p'").scalar_one()
        )
    engine.dispose()
