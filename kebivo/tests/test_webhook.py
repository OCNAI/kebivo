"""Webhook policy, payload and signing at the outbound boundary."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from kebivo.destinations.base import WebhookAdapter
from kebivo.destinations.payload import test_payload as make_test_payload
from kebivo.security.outbound import (
    BlockedDestination,
    allowed_address,
    post_pinned,
    resolve_checked,
)


def test_policy_ranges() -> None:
    for value in (
        "0.0.0.0",
        "169.254.169.254",
        "100.100.100.200",
        "168.63.129.16",
        "192.0.0.192",
        "fd00:ec2::254",
        "ff02::1",
    ):
        assert not allowed_address(ipaddress.ip_address(value), True)
    for value in (
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "192.0.2.1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:0a00:0001::",
        "2001:0:0:0:0:0:f5ff:fefe",
    ):
        assert not allowed_address(ipaddress.ip_address(value), False)
    assert allowed_address(ipaddress.ip_address("127.0.0.1"), True)
    assert allowed_address(ipaddress.ip_address("8.8.8.8"), False)


def test_mixed_resolution_blocks_every_answer(monkeypatch: Any) -> None:
    def mixed(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    try:
        asyncio.run(resolve_checked("example.test", 443, False))
    except BlockedDestination:
        pass
    else:
        raise AssertionError("mixed DNS answer accepted")


def test_checked_address_fallback_and_idn_host(monkeypatch: Any) -> None:
    hosts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            hosts.append(self.headers["Host"])
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.getaddrinfo

    def resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        if host in {"bücher.test", "xn--bcher-kva.test"}:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.2", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
            ]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    try:
        status, _ = asyncio.run(
            post_pinned(
                f"http://bücher.test:{server.server_port}/h", {}, b"{}", private=True, timeout=3
            )
        )
        assert status == 204
        assert hosts == [f"xn--bcher-kva.test:{server.server_port}"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_local_receiver_signed_payload_and_redirect(monkeypatch: Any) -> None:
    received: list[tuple[dict[str, str], bytes]] = []
    status = [204]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), body))
            self.send_response(status[0])
            if status[0] == 302:
                self.send_header("Location", "http://127.0.0.1/private")
            if status[0] == 429:
                self.send_header("Retry-After", "7")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret_bytes = b"test signing key with 32 bytes!!!!"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    original = socket.getaddrinfo

    def resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "receiver.test":
            return original("127.0.0.1", port, *args, **kwargs)
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    try:
        payload = make_test_payload()
        adapter = WebhookAdapter()
        config = {
            "auth": {"type": "bearer"},
            "custom_headers": ["X-Extra"],
            "signing": True,
            "allow_private_network": True,
        }
        secrets = {
            "url": f"http://receiver.test:{server.server_port}/hook",
            "token": "token-canary",
            "custom_headers.X-Extra": "header-canary",
            "signing_secret": secret,
        }
        result = asyncio.run(adapter.send(payload, config, secrets, timeout=3))
        assert result.outcome == "success" and result.category == "http_2xx"
        headers, body = received[0]
        assert headers["Host"] == f"receiver.test:{server.server_port}"
        assert headers["Authorization"] == "Bearer token-canary"
        schema = json.loads(
            (Path(__file__).parents[2] / "docs/webhook-payload.schema.json").read_text()
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(json.loads(body))
        assert json.loads(body) == payload

        def verifies(data: bytes, timestamp: str) -> bool:
            signed = f"{headers['webhook-id']}.{timestamp}.".encode() + data
            expected = (
                "v1,"
                + base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
            )
            return hmac.compare_digest(expected, headers["webhook-signature"])

        assert verifies(body, headers["webhook-timestamp"])
        assert not verifies(body + b" ", headers["webhook-timestamp"])
        assert not verifies(body, str(int(headers["webhook-timestamp"]) + 1))
        status[0] = 302
        result = asyncio.run(adapter.send(payload, config, secrets, timeout=3))
        assert result.outcome == "permanent" and result.category == "redirect_not_followed"
        assert len(received) == 2
        status[0] = 429
        result = asyncio.run(adapter.send(payload, config, secrets, timeout=3))
        assert result.category == "http_429" and result.retry_after == 7
        status[0] = 400
        result = asyncio.run(adapter.send(payload, config, secrets, timeout=3))
        assert result.outcome == "permanent" and result.category == "http_4xx"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_worker_retries_real_webhook_with_stable_signature(tmp_path: Path) -> None:
    from sqlalchemy import select

    from kebivo.models import Delivery, DeliveryAttempt, Destination
    from kebivo.security.secretbox import SecretBox
    from kebivo.tests.test_worker import due, route, setup_worker

    received: list[tuple[dict[str, str], bytes]] = []
    statuses = [500, 500, 200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(
                (dict(self.headers), self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(statuses.pop(0))
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker, factory, event_id, destination_id = setup_worker(tmp_path / "webhook-worker.db")
    secret_bytes = b"worker signing key with 32 bytes!!"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    try:
        with factory() as db:
            row = db.get(Destination, destination_id)
            assert row is not None
            row.type = "webhook"
            row.allow_private_network = True
            row.config_json = json.dumps(
                {
                    "auth": {"type": "none"},
                    "signing": True,
                    "custom_headers": [],
                    "timeout_seconds": 3,
                }
            )
            box = SecretBox.from_settings(worker.settings)
            row.secrets_key_id = box.current_key_id
            row.secrets_ciphertext = box.encrypt(
                destination_id,
                {"url": f"http://127.0.0.1:{server.server_port}/hook", "signing_secret": secret},
            )
            db.commit()
        delivery_id = route(worker, factory, event_id)

        async def deliver() -> None:
            for expected in ("retry_scheduled", "retry_scheduled", "delivered"):
                due(factory, delivery_id)
                assert worker._claim("delivery", 1) == [delivery_id]
                await worker._deliver(delivery_id)
                with factory() as db:
                    row = db.get(Delivery, delivery_id)
                    assert row is not None and row.status == expected

        asyncio.run(deliver())
        assert len(received) == 3
        schema = json.loads(
            (Path(__file__).parents[2] / "docs/webhook-payload.schema.json").read_text()
        )
        ids = set()
        for headers, body in received:
            payload = json.loads(body)
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
            assert payload["delivery_id"] == delivery_id
            ids.add(headers["webhook-id"])
            signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
            expected = (
                "v1,"
                + base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
            )
            assert hmac.compare_digest(expected, headers["webhook-signature"])
        assert ids == {delivery_id}
        with factory() as db:
            attempts = db.scalars(
                select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
            ).all()
            categories = [item.error_category for item in attempts]
            assert categories == ["http_5xx", "http_5xx", "http_2xx"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_rebinding_is_checked_on_each_attempt(monkeypatch: Any) -> None:
    answers = iter(("8.8.8.8", "127.0.0.1"))

    def changing(*args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (next(answers), 443))]

    monkeypatch.setattr(socket, "getaddrinfo", changing)
    assert asyncio.run(resolve_checked("receiver.test", 443, False)) == ["8.8.8.8"]
    try:
        asyncio.run(resolve_checked("receiver.test", 443, False))
    except BlockedDestination:
        pass
    else:
        raise AssertionError("rebinding answer accepted")


def test_webhook_test_send_uses_versioned_payload(tmp_path: Path) -> None:
    from kebivo.tests.test_projects import make_client, write_headers

    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client, _ = make_client(tmp_path / "test-send.db")
        headers = write_headers(client)
        created = client.post(
            "/api/v1/destinations",
            headers=headers,
            json={
                "name": "Test hook",
                "type": "webhook",
                "allow_private_network": True,
                "config": {
                    "url": f"http://127.0.0.1:{server.server_port}/hook",
                    "auth": {"type": "none"},
                    "signing": True,
                },
            },
        )
        assert created.status_code == 201, created.text
        result = client.post(f"/api/v1/destinations/{created.json()['id']}/test", headers=headers)
        assert result.status_code == 200 and result.json()["outcome"] == "success"
        schema = json.loads(
            (Path(__file__).parents[2] / "docs/webhook-payload.schema.json").read_text()
        )
        assert len(received) == 1
        payload = json.loads(received[0])
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
        assert payload["test"] is True
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
