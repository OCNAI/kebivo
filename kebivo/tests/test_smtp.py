"""SMTP transport, rendering, outcome mapping and worker integration."""

import asyncio
import json
import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from kebivo.destinations.payload import payload_v1
from kebivo.destinations.payload import test_payload as make_test_payload
from kebivo.destinations.smtp import SmtpAdapter
from kebivo.destinations.validation import ConfigError, validate_config
from kebivo.models import (
    Delivery,
    DeliveryAttempt,
    Destination,
    FeedbackEvent,
    FeedbackEventTag,
    Project,
    RoutingRule,
    RoutingRuleTag,
    Tag,
)
from kebivo.tests.test_worker import route, setup_worker

CANARY = "smtp-password-canary"


def test_smtp_blocks_metadata_resolution(monkeypatch: Any) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 587))
    ])
    result = asyncio.run(SmtpAdapter().send(
        make_test_payload(),
        {"host": "mail.example.test", "port": 587, "tls_mode": "none",
         "allow_private_network": True,
         "from": "sender@example.test", "to": ["to@example.test"]},
        {}, timeout=3,
    ))
    assert result.outcome == "permanent"
    assert result.category == "blocked_destination"


class MailHandler:
    def __init__(self, status: str = "250 OK") -> None:
        self.status = status
        self.messages: list[bytes] = []
        self.auth_count = 0

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        self.messages.append(envelope.content)
        return self.status

    async def handle_AUTH(self, server: Any, session: Any, envelope: Any, args: Any) -> str:
        self.auth_count += 1
        return "535 5.7.8 Authentication failed"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def server(handler: MailHandler, **options: Any) -> Iterator[int]:
    port = free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port, **options)
    controller.start()
    try:
        yield port
    finally:
        controller.stop()


@pytest.fixture
def tls_contexts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ssl.SSLContext:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    original = ssl.create_default_context

    def trusted_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        context = original(*args, cafile=str(cert_path), **kwargs)
        return context

    monkeypatch.setattr(ssl, "create_default_context", trusted_context)
    return server_context


def config(port: int, mode: str = "starttls") -> dict[str, Any]:
    # The test servers listen on loopback, so the destination opts into private networks.
    return {
        "allow_private_network": True,
        "host": "localhost",
        "port": port,
        "tls_mode": mode,
        "from": "sender@example.test",
        "to": ["receiver@example.test"],
        "timeout_seconds": 3,
    }


def payload(question: str = "Where is the guide?") -> dict[str, Any]:
    return payload_v1(
        delivery_id="86a24651-6db5-44a9-820a-0c1bde410b38",
        event_id="6482ddbd-ecde-43a6-b431-1dceac164dd5",
        event_type="knowledge_gap",
        project_id="acd2f038-7297-4328-a708-fd8f75301496",
        project_name="Support <team>",
        question=question,
        correction=None,
        context=None,
        tags=["customer-service"],
        created_at=datetime.now(UTC),
    )


def send(
    message: dict[str, Any], cfg: dict[str, Any], secrets: dict[str, Any] | None = None
) -> Any:
    return asyncio.run(SmtpAdapter().send(message, cfg, secrets or {}, timeout=3))


def parse(raw: bytes) -> Any:
    return BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[arg-type]


def test_falls_back_to_next_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some resolvers list ::1 before 127.0.0.1 for localhost; the server only has IPv4.
    original = socket.getaddrinfo

    def ipv6_first(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != "localhost":
            return original(host, *args, **kwargs)
        port = args[0] if args else kwargs["port"]
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", ipv6_first)
    handler = MailHandler()
    with server(handler) as port:
        result = send(payload(), config(port, "none"))
    assert result.outcome == "success" and len(handler.messages) == 1


def test_starttls_rendering_and_no_plaintext_fallback(tls_contexts: ssl.SSLContext) -> None:
    handler = MailHandler()
    with server(handler, tls_context=tls_contexts) as port:
        result = send(payload("Where?\nBcc: stolen@example.test <script>"), config(port))
    assert result.outcome == "success"
    assert len(handler.messages) == 1
    mail = parse(handler.messages[0])
    assert (
        mail["Subject"]
        == "[Knowledge Gap] Support <team> — Where? Bcc: stolen@example.test <script>"
    )
    assert mail["Bcc"] is None
    assert mail["X-Feedback-Delivery-Id"] == payload()["delivery_id"]
    assert mail["Message-ID"]
    plain = mail.get_body(preferencelist=("plain",)).get_content()
    html = mail.get_body(preferencelist=("html",)).get_content()
    assert "Project: Support <team>" in plain
    assert "Tags: customer-service" in plain
    assert "Occurrence count: 1" in plain
    assert "Submitted (UTC):" in plain
    assert "&lt;script&gt;" in html and "&lt;team&gt;" in html
    assert "<script>" not in html
    assert handler.auth_count == 0

    unavailable = MailHandler()
    with server(unavailable, auth_require_tls=False) as port:
        result = send(payload(), {**config(port), "username": "user"}, {"password": CANARY})
    assert result.outcome == "permanent" and result.category == "tls_unavailable"
    assert unavailable.auth_count == 0 and unavailable.messages == []
    assert CANARY not in str(result)


def test_implicit_tls_and_none(tls_contexts: ssl.SSLContext) -> None:
    implicit = MailHandler()
    with server(implicit, ssl_context=tls_contexts) as port:
        result = send(payload(), config(port, "implicit_tls"))
    assert result.outcome == "success" and len(implicit.messages) == 1

    plain = MailHandler()
    with server(plain) as port:
        result = send(payload(), config(port, "none"))
        bad = send(payload(), {**config(port, "none"), "username": "user"}, {"password": CANARY})
    assert result.outcome == "success" and len(plain.messages) == 1
    assert bad.outcome == "permanent" and bad.category == "config_invalid"
    with pytest.raises(ConfigError):
        validate_config({**config(25, "none"), "type": "smtp", "username": "user"}, {})


@pytest.mark.parametrize(
    ("reply", "outcome", "category"),
    [
        ("451 4.3.0 Try later", "retryable", "smtp_transient"),
        ("550 5.1.1 Rejected", "permanent", "smtp_permanent"),
    ],
)
def test_reply_mapping(reply: str, outcome: str, category: str) -> None:
    handler = MailHandler(reply)
    with server(handler) as port:
        result = send(payload(), config(port, "none"))
    assert result.outcome == outcome and result.category == category
    assert reply[:3] in (result.sanitized_error or "")
    assert "Try later" not in str(result) and "Rejected" not in str(result)
    assert CANARY not in str(result)


def test_auth_535(tls_contexts: ssl.SSLContext) -> None:
    handler = MailHandler()
    with server(handler, tls_context=tls_contexts, auth_require_tls=False) as port:
        result = send(payload(), {**config(port), "username": "user"}, {"password": CANARY})
    assert result.outcome == "permanent" and result.category == "smtp_auth"
    assert handler.auth_count >= 1 and not handler.messages
    assert CANARY not in str(result)


def test_test_message() -> None:
    handler = MailHandler()
    with server(handler) as port:
        result = send(make_test_payload(), config(port, "none"))
    assert result.outcome == "success"
    mail = parse(handler.messages[0])
    assert mail["Subject"].startswith("[TEST] [Knowledge Gap]")
    assert (
        mail.get_body(preferencelist=("plain",)).get_content().startswith("This is a test message")
    )


def test_worker_routes_default_tag_to_one_mail(tmp_path: Path) -> None:
    worker, factory, event_id, destination_id = setup_worker(tmp_path / "smtp-worker.db")
    handler = MailHandler()
    with server(handler) as port:
        with factory() as db:
            event = db.get(FeedbackEvent, event_id)
            destination = db.get(Destination, destination_id)
            assert event is not None and destination is not None
            project = db.get(Project, event.project_id)
            rule = db.scalar(select(RoutingRule).where(RoutingRule.project_id == event.project_id))
            assert project is not None and rule is not None
            project.name = "Support"
            tag = Tag(
                project_id=project.id,
                name="customer-service",
                slug="customer-service",
                is_default=True,
            )
            db.add(tag)
            db.flush()
            db.add_all(
                [
                    FeedbackEventTag(event_id=event.id, tag_id=tag.id, from_default=True),
                    RoutingRuleTag(rule_id=rule.id, tag_id=tag.id),
                ]
            )
            destination.type = "smtp"
            destination.allow_private_network = True
            destination.config_json = json.dumps(config(port, "none"))
            db.commit()
        worker.settings = replace(worker.settings, allowed_origins=("https://app.example.test",))
        delivery_id = route(worker, factory, event_id)
        assert worker._claim("delivery", 1) == [delivery_id]
        asyncio.run(worker._deliver(delivery_id))
    assert len(handler.messages) == 1
    mail = parse(handler.messages[0])
    assert mail["Subject"] == "[Knowledge Gap] Support — Question"
    assert mail["X-Feedback-Delivery-Id"] == delivery_id
    body = mail.get_body(preferencelist=("plain",)).get_content()
    assert "Tags: customer-service" in body
    assert f"Dashboard: https://app.example.test/deliveries?delivery_id={delivery_id}" in body
    with factory() as db:
        delivery = db.get(Delivery, delivery_id)
        assert delivery is not None and delivery.status == "delivered"
        assert (
            len(
                db.scalars(
                    select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
                ).all()
            )
            == 1
        )


def test_untrusted_certificate_is_terminal(
    tls_contexts: ssl.SSLContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = MailHandler()
    with server(handler, tls_context=tls_contexts) as port:
        monkeypatch.setattr(
            ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        )
        result = send(payload(), config(port))
    assert result.outcome == "permanent" and result.category == "tls"
    assert handler.messages == []


def test_admin_test_send_creates_no_event(tmp_path: Path) -> None:
    from sqlalchemy import func

    from kebivo.tests.test_projects import make_client, write_headers

    handler = MailHandler()
    with server(handler) as port:
        client, app = make_client(tmp_path / "smtp-test-send.db")
        headers = write_headers(client)
        created = client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "name": "Test SMTP",
                "type": "smtp",
                "allow_private_network": True,
                "config": config(port, "none"),
            },
        )
        assert created.status_code == 201, created.text
        response = client.post(
            f"/api/v1/destinations/{created.json()['id']}/test", headers=headers
        )
        assert response.status_code == 200 and response.json()["outcome"] == "success"
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(FeedbackEvent)) == 0
            assert db.scalar(select(func.count()).select_from(Delivery)) == 0
    assert len(handler.messages) == 1
    assert parse(handler.messages[0])["Subject"].startswith("[TEST]")


ALWAYS_BLOCKED = (
    "0.0.0.0",
    "169.254.169.254",
    "100.100.100.200",
    "168.63.129.16",
    "192.0.0.192",
    "fd00:ec2::254",
    "ff02::1",
)
PRIVATE_ONLY = (
    "127.0.0.1",
    "10.0.0.1",
    "100.64.0.1",
    "192.0.2.1",
    "fc00::1",
    "::ffff:127.0.0.1",
    "64:ff9b::a00:1",
    "2002:0a00:0001::",
    "2001:0:0:0:0:0:f5ff:fefe",
)


def policy_send(
    monkeypatch: pytest.MonkeyPatch, address: str, private: bool
) -> tuple[Any, list[tuple[str, int]]]:
    """Send to a host resolving to ``address``; record connections instead of opening them."""
    connected: list[tuple[str, int]] = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 587))],
    )

    def refuse(target: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        connected.append(target)
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", refuse)
    cfg = {**config(587), "host": "mail.example.test", "allow_private_network": private}
    return send(payload(), cfg), connected


@pytest.mark.parametrize("address", ALWAYS_BLOCKED)
@pytest.mark.parametrize("private", [False, True])
def test_smtp_policy_always_blocked(
    monkeypatch: pytest.MonkeyPatch, address: str, private: bool
) -> None:
    result, connected = policy_send(monkeypatch, address, private)
    assert (result.outcome, result.category) == ("permanent", "blocked_destination")
    assert connected == []


@pytest.mark.parametrize("address", PRIVATE_ONLY)
def test_smtp_policy_private_needs_flag(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    result, connected = policy_send(monkeypatch, address, False)
    assert (result.outcome, result.category) == ("permanent", "blocked_destination")
    assert connected == []
    result, connected = policy_send(monkeypatch, address, True)
    assert result.category == "smtp_transient"
    assert connected == [(address, 587)]


def test_smtp_policy_public_address_connects_to_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, connected = policy_send(monkeypatch, "8.8.8.8", False)
    assert result.category == "smtp_transient"
    assert connected == [("8.8.8.8", 587)]


def test_smtp_mixed_resolution_blocks_every_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 587)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 587)),
        ],
    )
    cfg = {**config(587), "host": "mail.example.test", "allow_private_network": False}
    result = send(payload(), cfg)
    assert result.category == "blocked_destination"


def test_smtp_rebinding_after_check_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name is resolved once: a second answer pointing at metadata is never used."""
    handler = MailHandler()
    lookups = 0
    original = socket.getaddrinfo

    def rebinding(host: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal lookups
        if host != "rebind.example.test":
            return original(host, *args, **kwargs)
        lookups += 1
        address = "127.0.0.1" if lookups == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, args[0]))]

    with server(handler) as port:
        monkeypatch.setattr(socket, "getaddrinfo", rebinding)
        result = send(payload(), {**config(port, "none"), "host": "rebind.example.test"})
    assert result.outcome == "success" and len(handler.messages) == 1
    assert lookups == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"tls_mode": "none"}, "tls_mode"),
        ({"port": 1025}, "port"),
        ({"port": 22}, "port"),
    ],
)
def test_public_smtp_needs_tls_and_a_standard_port(changes: dict[str, Any], field: str) -> None:
    public = {**config(587), "type": "smtp", "allow_private_network": False, **changes}
    with pytest.raises(ConfigError) as error:
        validate_config(public, {})
    assert error.value.field == field
    for port in (25, 465, 587, 2525):
        validate_config({**public, "tls_mode": "starttls", "port": port}, {})
    validate_config({**public, "allow_private_network": True}, {})


def test_smtp_destination_accepts_private_network_option(tmp_path: Path) -> None:
    from kebivo.tests.test_projects import make_client, write_headers

    client, _ = make_client(tmp_path / "smtp-private.db")
    headers = write_headers(client)
    relay = config(1025, "none")
    relay.pop("allow_private_network")
    body = {"name": "Relay", "type": "smtp", "config": relay}
    refused = client.post("/api/v1/destinations", headers=headers, json=body)
    assert refused.status_code == 422
    created = client.post(
        "/api/v1/destinations", headers=headers, json={**body, "allow_private_network": True}
    )
    assert created.status_code == 201, created.text
    assert created.json()["allow_private_network"] is True
    turned_off = client.patch(
        f"/api/v1/destinations/{created.json()['id']}",
        headers=headers,
        json={"allow_private_network": False},
    )
    assert turned_off.status_code == 422
