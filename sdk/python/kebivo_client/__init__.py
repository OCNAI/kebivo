"""Small, synchronous client for submitting Kebivo feedback."""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import socket
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Event, Timer
from typing import Any, cast
from urllib.parse import urlsplit

__all__ = [
    "Kebivo",
    "KebivoError",
    "KebivoHTTPError",
    "KebivoConnectionError",
    "KebivoValidationError",
]

logger = logging.getLogger(__name__)
_warned_insecure = False


class KebivoError(Exception):
    """Base error for feedback submission failures."""


class KebivoHTTPError(KebivoError):
    """An HTTP response rejected a submission, including exhausted retries."""

    def __init__(
        self,
        status_code: int,
        code: str | None,
        message: str,
        details: Any = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id
        super().__init__(f"HTTP {status_code}: {message}")


class KebivoConnectionError(KebivoError):
    """No HTTP response was received after all attempts."""


class KebivoValidationError(KebivoError, ValueError):
    """A request parameter is invalid."""


MAX_RETRY_AFTER = 60.0


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()  # noqa: UP017
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return min(MAX_RETRY_AFTER, max(0.0, seconds))


class Kebivo:
    """Submit gaps and proposed corrections using a project token."""

    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise KebivoValidationError("url must be an HTTP(S) base URL without userinfo")
        if not token:
            raise KebivoValidationError("token is required")
        if not math.isfinite(timeout) or timeout <= 0:
            raise KebivoValidationError("timeout must be finite and positive")
        global _warned_insecure
        if parsed.scheme == "http" and not _warned_insecure:
            host = parsed.hostname or ""
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                logger.warning("Kebivo token will be sent over HTTP to a non-loopback host")
                _warned_insecure = True
        self.url = url.rstrip("/") + "/api/v1/feedback"
        self.token = token
        self.timeout = timeout

    def report_gap(
        self,
        question: str,
        *,
        tags: list[str] | None = None,
        previous_answer: str | None = None,
        context: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        source_event_id: str | None = None,
        user_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            "knowledge_gap",
            question,
            tags,
            previous_answer,
            context,
            agent_id,
            conversation_id,
            source_event_id,
            user_reference,
            metadata,
            idempotency_key,
        )

    def submit_correction(
        self,
        question: str,
        correction: str,
        *,
        tags: list[str] | None = None,
        source_reference: str | None = None,
        previous_answer: str | None = None,
        context: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        source_event_id: str | None = None,
        user_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            "knowledge_correction",
            question,
            tags,
            previous_answer,
            context,
            agent_id,
            conversation_id,
            source_event_id,
            user_reference,
            metadata,
            idempotency_key,
            correction=correction,
            source_reference=source_reference,
        )

    def _submit(
        self,
        event_type: str,
        question: str,
        tags: list[str] | None,
        previous_answer: str | None,
        context: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        source_event_id: str | None,
        user_reference: str | None,
        metadata: dict[str, Any] | None,
        idempotency_key: str | None,
        **extra: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"event_type": event_type, "question": question}
        optional = {
            "tags": tags,
            "previous_answer": previous_answer,
            "context": context,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "source_event_id": source_event_id,
            "user_reference": user_reference,
            "metadata": metadata,
            **extra,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        data = json.dumps(body, allow_nan=False).encode("utf-8")
        key = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        if not 1 <= len(key) <= 255 or any(not 33 <= ord(char) <= 126 for char in key):
            raise KebivoValidationError(
                "idempotency_key must be 1-255 printable ASCII characters without spaces"
            )
        for attempt in range(4):
            try:
                status, headers, raw = self._request(data, key)
                if 200 <= status < 300:
                    try:
                        return cast(dict[str, Any], json.loads(raw))
                    except ValueError as error:
                        raise KebivoError("Kebivo returned a response that is not JSON") from error
                try:
                    envelope = json.loads(raw).get("error", {})
                except (ValueError, AttributeError):
                    envelope = {}
                message = envelope.get("message") or "Request failed"
                failure = KebivoHTTPError(
                    status,
                    envelope.get("code"),
                    str(message),
                    envelope.get("details"),
                    envelope.get("request_id") or headers.get("X-Request-ID"),
                )
                if status != 429 and status < 500 or attempt == 3:
                    raise failure
                wait = max(2**attempt, _retry_after(headers.get("Retry-After")))
            except TimeoutError as error:
                raise KebivoConnectionError(str(error)) from error
            except (OSError, http.client.HTTPException) as error:
                if attempt == 3:
                    raise KebivoConnectionError(str(error)) from error
                wait = 2**attempt
            time.sleep(wait)
        raise AssertionError("retry loop must return or raise")

    def _request(self, data: bytes, key: str) -> tuple[int, http.client.HTTPMessage, bytes]:
        parsed = urlsplit(self.url)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        assert parsed.hostname is not None
        connection = connection_type(parsed.hostname, parsed.port, timeout=self.timeout)
        expired = Event()
        active_socket: socket.socket | None = None

        def stop() -> None:
            expired.set()
            sock = active_socket or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = Timer(self.timeout, stop)
        timer.daemon = True
        timer.start()
        try:
            path = parsed.path or "/"
            connection.request(
                "POST",
                path,
                body=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": key,
                },
            )
            if expired.is_set():
                raise TimeoutError("Kebivo request deadline exceeded")
            active_socket = connection.sock
            response = connection.getresponse()
            raw = response.read()
            if expired.is_set():
                raise TimeoutError("Kebivo request deadline exceeded")
            return response.status, response.headers, raw
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise TimeoutError("Kebivo request deadline exceeded") from None
            raise
        finally:
            timer.cancel()
            connection.close()
