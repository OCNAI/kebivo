"""Small, synchronous client for submitting Kebivo feedback."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit

__all__ = ["Kebivo", "KebivoError", "KebivoHTTPError", "KebivoConnectionError"]


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


MAX_RETRY_AFTER = 60.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend the bearer token to the redirect target.
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


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
        ):
            raise ValueError("url must be an HTTP(S) base URL")
        if not token:
            raise ValueError("token is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
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
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
            method="POST",
        )
        for attempt in range(4):
            try:
                with _OPENER.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                try:
                    return cast(dict[str, Any], json.loads(raw))
                except ValueError as error:
                    raise KebivoError("Kebivo returned a response that is not JSON") from error
            except urllib.error.HTTPError as error:
                raw = error.read()
                try:
                    envelope = json.loads(raw).get("error", {})
                except (ValueError, AttributeError):
                    envelope = {}
                message = envelope.get("message") or error.reason or "Request failed"
                failure = KebivoHTTPError(
                    error.code,
                    envelope.get("code"),
                    str(message),
                    envelope.get("details"),
                    envelope.get("request_id") or error.headers.get("X-Request-ID"),
                )
                if error.code != 429 and error.code < 500 or attempt == 3:
                    raise failure from error
                wait = max(2**attempt, _retry_after(error.headers.get("Retry-After")))
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                ConnectionError,
            ) as error:
                if attempt == 3:
                    raise KebivoConnectionError(str(error)) from error
                wait = 2**attempt
            time.sleep(wait)
        raise AssertionError("retry loop must return or raise")
