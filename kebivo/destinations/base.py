"""Bounded delivery adapter contract and registry."""

import base64
import hashlib
import hmac
import json
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

import httpx

from kebivo.destinations.smtp import SmtpAdapter
from kebivo.destinations.validation import validate_config
from kebivo.security.outbound import BlockedDestination, post_pinned

Outcome = Literal["success", "retryable", "permanent"]
ERRORS = {
    "timeout": "Destination timed out",
    "dns": "Destination DNS lookup failed",
    "connect": "Destination connection failed",
    "tls": "Destination TLS handshake failed",
    "blocked_destination": "Destination address is blocked",
    "redirect_not_followed": "Destination redirect was not followed",
    "http_2xx": "Destination accepted delivery",
    "http_4xx": "Destination rejected delivery",
    "http_408": "Destination request timed out",
    "http_429": "Destination rate limited delivery",
    "http_5xx": "Destination temporarily rejected delivery",
    "network": "Destination connection failed",
    "http_retryable": "Destination temporarily rejected delivery",
    "http_permanent": "Destination rejected delivery",
    "smtp_transient": "Mail server temporarily rejected delivery",
    "smtp_auth": "Mail server authentication failed",
    "tls_unavailable": "Mail server does not support STARTTLS",
    "smtp_permanent": "Mail server rejected delivery",
    "secret_unavailable": "Destination secret unavailable",
    "config_invalid": "Destination configuration invalid",
    "adapter_error": "Destination delivery failed",
}


@dataclass(frozen=True)
class DeliveryResult:
    outcome: Outcome
    category: str | None = None
    http_status: int | None = None
    retry_after: int | None = None
    sanitized_error: str | None = None


def failure(
    outcome: Literal["retryable", "permanent"],
    category: str,
    *,
    http_status: int | None = None,
    retry_after: int | None = None,
) -> DeliveryResult:
    sanitized = ERRORS[category]
    if category.startswith("smtp_") and http_status is not None:
        sanitized += f" (SMTP {http_status})"
    return DeliveryResult(outcome, category, http_status, retry_after, sanitized)


class Adapter(Protocol):
    async def send(
        self,
        message: dict[str, Any],
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
    ) -> DeliveryResult: ...


class InboxAdapter:
    async def send(
        self,
        message: dict[str, Any],
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
    ) -> DeliveryResult:
        return DeliveryResult("success")


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(
                0,
                round(
                    (
                        parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
                    ).total_seconds()
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return None


class WebhookAdapter:
    async def send(
        self,
        message: dict[str, Any],
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
    ) -> DeliveryResult:
        try:
            body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
            delivery_id = str(message["delivery_id"])
            headers = {
                "Content-Type": "application/json",
                "User-Agent": config.get("user_agent", "Kebivo-webhook/1"),
                "X-Delivery-ID": delivery_id,
            }
            for name in config.get("custom_headers", []):
                headers[name] = secrets[f"custom_headers.{name}"]
            auth = config.get("auth", {"type": "none"})
            if auth["type"] == "bearer":
                headers["Authorization"] = "Bearer " + secrets["token"]
            elif auth["type"] == "header":
                headers[auth["name"]] = secrets["header_value"]
            if config.get("signing"):
                timestamp = str(int(time.time()))
                secret = base64.b64decode(
                    secrets["signing_secret"].removeprefix("whsec_"), validate=True
                )
                signature = hmac.new(
                    secret, f"{delivery_id}.{timestamp}.".encode() + body, hashlib.sha256
                ).digest()
                headers.update(
                    {
                        "webhook-id": delivery_id,
                        "webhook-timestamp": timestamp,
                        "webhook-signature": "v1," + base64.b64encode(signature).decode(),
                    }
                )
            status, retry_header = await post_pinned(
                secrets["url"],
                headers,
                body,
                private=bool(config.get("allow_private_network")),
                timeout=timeout,
            )
        except BlockedDestination:
            return failure("permanent", "blocked_destination")
        except (TimeoutError, httpx.TimeoutException):
            return failure("retryable", "timeout")
        except socket.gaierror:
            return failure("retryable", "dns")
        except UnicodeError:
            return failure("permanent", "config_invalid")
        except httpx.ConnectError as exc:
            cause: BaseException | None = exc
            tls_error = False
            while cause is not None:
                tls_error |= isinstance(cause, ssl.SSLError)
                cause = cause.__cause__
            return failure("retryable", "tls" if tls_error else "connect")
        except (httpx.NetworkError, OSError):
            return failure("retryable", "connect")
        except httpx.ProtocolError:
            return failure("permanent", "config_invalid")
        except (KeyError, ValueError, TypeError):
            return failure("permanent", "config_invalid")
        if 200 <= status < 300:
            return DeliveryResult("success", "http_2xx", status)
        if 300 <= status < 400:
            return failure("permanent", "redirect_not_followed", http_status=status)
        if status == 408:
            return failure(
                "retryable", "http_408", http_status=status, retry_after=_retry_after(retry_header)
            )
        if status == 429:
            return failure(
                "retryable", "http_429", http_status=status, retry_after=_retry_after(retry_header)
            )
        if status >= 500:
            return failure(
                "retryable", "http_5xx", http_status=status, retry_after=_retry_after(retry_header)
            )
        return failure("permanent", "http_4xx", http_status=status)


class SlackAdapter(WebhookAdapter):
    async def send(
        self,
        message: dict[str, Any],
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
    ) -> DeliveryResult:
        question = str(message["feedback"]["question"])
        # Slack rejects section text over 3000 characters; questions may be longer.
        if len(question) > 3000:
            question = question[:2999] + "…"
        project = str(message["project"]["name"])
        tags = ", ".join(str(tag) for tag in message["tags"]) or "None"
        fields = [
            {"type": "plain_text", "text": f"Project: {project}"},
            {"type": "plain_text", "text": f"Tags: {tags}"},
            {"type": "plain_text", "text": f"Occurrences: {message['occurrence_count']}"},
        ]
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "plain_text", "text": question}},
            {"type": "section", "fields": fields},
        ]
        public_url = config.get("public_url")
        if public_url and message.get("group_id") and not message.get("test"):
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "View group"},
                            "url": f"{public_url}/inbox/groups/{message['group_id']}",
                        }
                    ],
                }
            )
        return await super().send(
            {
                "delivery_id": message["delivery_id"],
                "text": "New Kebivo feedback",
                "blocks": blocks,
            },
            config,
            secrets,
            timeout=timeout,
        )


REGISTRY: dict[str, Adapter] = {
    "inbox": InboxAdapter(),
    "webhook": WebhookAdapter(),
    "slack": SlackAdapter(),
    "smtp": SmtpAdapter(),
}

__all__ = ["Adapter", "DeliveryResult", "REGISTRY", "failure", "validate_config"]
