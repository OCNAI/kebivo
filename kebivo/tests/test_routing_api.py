"""Routing configuration, simulation and secret boundaries."""

import smtplib
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from kebivo.models import AdminSession, AuditEvent, Base, Destination
from kebivo.routing.engine import DestinationInput, RuleInput, evaluate
from kebivo.tests.test_projects import make_client, write_headers


def rule(identifier: str, **changes: object) -> RuleInput:
    base = RuleInput(
        identifier,
        identifier,
        10,
        datetime(2026, 1, 1, tzinfo=UTC),
        True,
        ("knowledge_gap",),
        "any",
        ("bug",),
        ("d1",),
        "continue",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mode,tags,present,matched",
    [
        ("any", ("bug", "urgent"), {"bug"}, True),
        ("any", ("bug", "urgent"), set(), False),
        ("all", ("bug", "urgent"), {"bug"}, False),
        ("all", ("bug", "urgent"), {"bug", "urgent"}, True),
        ("all", (), set(), True),
    ],
)
def test_evaluate_tag_modes(
    mode: str, tags: tuple[str, ...], present: set[str], matched: bool
) -> None:
    result = evaluate(
        "knowledge_gap", present, [rule("r", match_mode=mode, tags=tags)], [DestinationInput("d1")]
    )
    assert (result.evaluated_rules[0].status == "matched") is matched


def test_evaluate_order_stop_duplicates_disabled_and_fallback() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rules = [
        rule("z", created_at=created, destination_ids=("d1", "d2"), match_behavior="stop"),
        rule("a", created_at=created, destination_ids=("d1",)),
        rule("early", priority=0, enabled=False),
    ]
    destinations = [DestinationInput("d1"), DestinationInput("d2", False)]
    result = evaluate("knowledge_gap", {"bug"}, rules, destinations, "d2")
    assert [item.rule_id for item in result.evaluated_rules] == ["early", "a", "z"]
    assert [item.status for item in result.evaluated_rules] == [
        "skipped_disabled",
        "matched",
        "matched",
    ]
    assert [item.outcome for item in result.destinations] == [
        "delivery_created",
        "duplicate_destination",
        "destination_disabled",
    ]
    assert result.stopped_by_rule_id == "z" and not result.fallback_applied
    assert (
        evaluate("knowledge_correction", set(), rules, destinations, "d2").destinations[0].outcome
        == "fallback_skipped_disabled"
    )
    assert not evaluate("knowledge_correction", set(), rules, destinations).fallback_applied
    disabled = evaluate(
        "knowledge_gap", {"bug"}, [rule("r", destination_ids=("d2",))], destinations, "d1"
    )
    assert disabled.warnings and not disabled.fallback_applied


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/secret-canary-path",
        "http://100.100.100.200/secret-canary-path",
        "http://0.0.0.1/secret-canary-path",
        "http://[fe80::1]/secret-canary-path",
        "http://[fd00:ec2::254]/secret-canary-path",
    ],
)
def test_webhook_rejects_always_blocked_ip_at_save(tmp_path: Path, url: str) -> None:
    client, _ = make_client(tmp_path / "blocked-webhook.db")
    headers = write_headers(client)
    payload = {
        "name": "Hook",
        "type": "webhook",
        "allow_private_network": True,
        "config": {"url": url},
    }
    created = client.post("/api/v1/destinations", json=payload, headers=headers)
    assert created.status_code == 422, created.text
    assert created.json()["error"]["details"]["fields"][0]["field"] == "url"


def test_webhook_private_ip_opt_in_and_patch_validation(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path / "private-webhook.db")
    headers = write_headers(client)
    created = client.post(
        "/api/v1/destinations",
        json={
            "name": "Private hook",
            "type": "webhook",
            "allow_private_network": True,
            "config": {"url": "http://10.0.0.1/hook"},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    path = f"/api/v1/destinations/{created.json()['id']}"
    rejected = client.patch(
        path,
        json={"config": {"url": "http://169.254.169.254/secret-canary-path"}},
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["details"]["fields"][0]["field"] == "url"


def test_destination_rule_simulator_and_secret_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = make_client(tmp_path / "routing.db")
    headers = write_headers(client)
    project = client.post(
        "/api/v1/projects", json={"name": "P", "slug": "p"}, headers=headers
    ).json()
    canary = "private-canary-value"
    payload = {
        "name": "Hook",
        "type": "webhook",
        "config": {
            "url": f"https://example.test/{canary}",
            "auth": {"type": "bearer", "token": canary},
            "signing": True,
            "custom_headers": {"X-Secret": canary},
        },
    }
    assert client.post("/api/v1/destinations", json=payload).status_code == 403
    response = client.post("/api/v1/destinations", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    destination = response.json()
    assert destination["signing_secret"].startswith("whsec_")
    identifier = destination["id"]
    assert canary not in response.text
    assert canary not in client.get(f"/api/v1/destinations/{identifier}").text
    assert canary not in client.get("/api/v1/destinations").text
    assert (
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"fallback_destination_id": identifier},
            headers=headers,
        ).status_code
        == 200
    )
    rule_body = {
        "name": "Bug route",
        "event_types": ["knowledge_gap"],
        "match_mode": "any",
        "tags": ["Bug"],
        "priority": 10,
        "destination_ids": [identifier],
        "match_behavior": "stop",
    }
    path = f"/api/v1/projects/{project['id']}/routing-rules"
    created = client.post(path, json=rule_body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["tags"] == ["bug"]
    assert client.delete(f"/api/v1/destinations/{identifier}", headers=headers).status_code == 409

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access during simulation")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(smtplib.SMTP, "connect", forbidden)
    with app.state.session_factory() as db:
        session_before = db.scalar(select(AdminSession.last_seen_at))
        before = {
            name: db.execute(
                select(__import__("sqlalchemy").func.count()).select_from(table)
            ).scalar_one()
            for name, table in Base.metadata.tables.items()
        }
    simulation = client.post(
        f"/api/v1/projects/{project['id']}/routing/simulate",
        json={"event_type": "knowledge_gap", "tags": ["Bug"]},
        headers=headers,
    )
    assert simulation.status_code == 200, simulation.text
    assert simulation.json()["destinations"][0]["outcome"] == "delivery_created"
    with app.state.session_factory() as db:
        assert db.scalar(select(AdminSession.last_seen_at)) == session_before
        after = {
            name: db.execute(
                select(__import__("sqlalchemy").func.count()).select_from(table)
            ).scalar_one()
            for name, table in Base.metadata.tables.items()
        }
        assert before == after
        assert all(canary not in row.details_json for row in db.scalars(select(AuditEvent)))
    client.cookies.clear()
    assert (
        client.get("/api/v1/destinations", headers={"Authorization": "Bearer token"}).status_code
        == 401
    )
    assert client.get(path, headers={"Authorization": "Bearer token"}).status_code == 401
    app.state.engine.dispose()


def test_evaluate_stop_event_filter_and_creation_ties() -> None:
    first = rule("a", priority=0, match_behavior="stop")
    same = rule("b", priority=0)
    later = rule("c", priority=1)
    result = evaluate("knowledge_gap", {"bug"}, [later, same, first], [DestinationInput("d1")])
    assert [item.rule_id for item in result.evaluated_rules] == ["a", "b", "c"]
    assert [item.status for item in result.evaluated_rules] == [
        "matched",
        "not_evaluated_stopped",
        "not_evaluated_stopped",
    ]
    filtered = evaluate("knowledge_correction", {"bug"}, [first], [DestinationInput("d1")])
    assert filtered.evaluated_rules[0].reason == "Event type does not match"


def test_destination_validation_fields() -> None:
    from kebivo.destinations.validation import ConfigError, validate_config

    base = {"type": "webhook", "url": "https://example.test/x"}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"url": "http://example.test"}, "url"),
        ({"url": "https://localhost/"}, "url"),
        ({"url": "https://127.0.0.1/"}, "url"),
        ({"url": "https://user@example.test/"}, "url"),
        ({"url": "https://example.test/#frag"}, "url"),
        ({"timeout_seconds": 31}, "timeout_seconds"),
        ({"custom_headers": {"Host": "x"}}, "custom_headers"),
        ({"custom_headers": {"X-Bad\n": "x"}}, "custom_headers"),
        ({"auth": {"type": "bearer"}}, "auth.token"),
        ({"auth": {"type": "bearer", "token": "bad\x00token"}}, "auth.token"),
        ({"auth": {"type": "header", "name": "X-Auth", "value": "bad\x01value"}}, "auth.value"),
        ({"custom_headers": {"X-Auth": "bad\x7fvalue"}}, "custom_headers.X-Auth"),
    ]
    for change, field in cases:
        with pytest.raises(ConfigError) as failure:
            validate_config({**base, **change}, {})
        assert failure.value.field == field
    smtp = {
        "type": "smtp",
        "host": "example.test",
        "port": 587,
        "from": "sender@example.test",
        "to": ["to@example.test"],
    }
    smtp_cases: list[tuple[dict[str, Any], str]] = [
        ({"port": 0}, "port"),
        ({"tls_mode": "none", "password": "canary", "allow_private_network": True}, "password"),
        ({"tls_mode": "none"}, "tls_mode"),
        ({"port": 1025}, "port"),
        ({"to": []}, "to"),
        ({"from": "bad"}, "from"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
    ]
    for change, field in smtp_cases:
        with pytest.raises(ConfigError) as failure:
            validate_config({**smtp, **change}, {})
        assert failure.value.field == field


def test_corrupt_secret_does_not_break_destination_list(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "corrupt-secret.db")
    headers = write_headers(client)
    created = client.post(
        "/api/v1/destinations",
        json={"name": "Hook", "type": "webhook", "config": {"url": "https://example.test/h"}},
        headers=headers,
    )
    assert created.status_code == 201
    identifier = created.json()["id"]
    with app.state.session_factory() as db:
        row = db.get(Destination, identifier)
        assert row is not None
        row.secrets_ciphertext = b"corrupted"
        db.commit()
    listed = client.get("/api/v1/destinations")
    assert listed.status_code == 200
    assert (
        next(item for item in listed.json()["items"] if item["id"] == identifier)[
            "secrets_unavailable"
        ]
        is True
    )
    patched = client.patch(
        f"/api/v1/destinations/{identifier}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["secrets_unavailable"] is True
    assert (
        client.patch(
            f"/api/v1/destinations/{identifier}",
            json={"config": {"timeout_seconds": 3}},
            headers=headers,
        ).status_code
        == 409
    )


def test_smtp_secret_patch_and_system_inbox_controls(tmp_path: Path) -> None:
    client, app = make_client(tmp_path / "smtp.db")
    headers = write_headers(client)
    with app.state.session_factory() as db:
        db.add(Destination(name="Built-in inbox", type="inbox", is_system=True))
        db.commit()
    canary = "smtp-password-canary"
    created = client.post(
        "/api/v1/destinations",
        json={
            "name": "Mail",
            "type": "smtp",
            "config": {
                "host": "mail.example.test",
                "port": 587,
                "from": "from@example.test",
                "to": ["to@example.test"],
                "username": "mailer",
                "password": canary,
            },
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    assert canary not in created.text
    assert created.json()["config"]["password"] == {"set": True}
    patched = client.patch(
        f"/api/v1/destinations/{identifier}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["config"]["password"] == {"set": True}
    cleared = client.patch(
        f"/api/v1/destinations/{identifier}", json={"config": {"password": None}}, headers=headers
    )
    assert cleared.status_code == 200, cleared.text
    assert "password" not in cleared.json()["config"]
    inbox = next(
        item for item in client.get("/api/v1/destinations").json()["items"] if item["is_system"]
    )
    path = f"/api/v1/destinations/{inbox['id']}"
    assert client.patch(path, json={"name": "Other"}, headers=headers).status_code == 422
    assert client.delete(path, headers=headers).status_code == 422
    assert client.patch(path, json={"enabled": False}, headers=headers).status_code == 200
    app.state.engine.dispose()
