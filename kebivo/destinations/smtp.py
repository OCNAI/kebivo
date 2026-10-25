"""SMTP delivery with explicit transport security and bounded, sanitized results."""

import asyncio
import html
import ipaddress
import smtplib
import socket
import ssl
import unicodedata
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any
from urllib.parse import quote

from kebivo.destinations.validation import ConfigError, validate_config
from kebivo.security.outbound import allowed_address


class _PinnedSMTP(smtplib.SMTP):
    """Connect only to addresses that passed the outbound policy, never a fresh lookup.

    smtplib keeps the configured host name for EHLO, STARTTLS and implicit TLS
    certificate checks; only the TCP connection uses the pinned addresses, tried
    in resolver order like socket.create_connection.
    """

    def __init__(
        self,
        addresses: list[str],
        host: str,
        port: int,
        *,
        timeout: int,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self._pinned_addresses = addresses
        self._implicit_tls = context
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        error: OSError | None = None
        for address in self._pinned_addresses:
            try:
                sock = socket.create_connection((address, port), timeout, self.source_address)
                break
            except OSError as exc:
                error = exc
        else:
            raise error or OSError("no pinned address")
        if self._implicit_tls is not None:
            sock = self._implicit_tls.wrap_socket(sock, server_hostname=host)
        return sock


def _header(value: object) -> str:
    cleaned = "".join(" " if unicodedata.category(char) == "Cc" else char for char in str(value))
    return " ".join(cleaned.split())


def _mail(message: dict[str, Any], config: dict[str, Any]) -> EmailMessage:
    project = str(message["project"]["name"])
    feedback = message["feedback"]
    event_type = str(message["event_type"])
    label = {"knowledge_gap": "Knowledge Gap", "knowledge_correction": "Knowledge Correction"}[
        event_type
    ]
    question = str(feedback["question"])
    correction = feedback.get("correction")
    tags = message["tags"]
    submitted = datetime.fromisoformat(message["created_at"]).astimezone(UTC).isoformat()
    delivery_id = str(message["delivery_id"])
    test = message.get("test") is True
    mail = EmailMessage(policy=SMTP)
    mail["From"] = config["from"]
    mail["To"] = ", ".join(config["to"])
    subject = f"[{label}] {_header(project)} — {_header(question)[:60]}"
    mail["Subject"] = f"[TEST] {subject}" if test else subject
    mail["Message-ID"] = f"<{delivery_id}@feedback.local>"
    mail["X-Feedback-Delivery-Id"] = delivery_id
    lines = []
    if test:
        lines.append("This is a test message")
    lines.extend(
        [
            f"Project: {project}",
            f"Event type: {event_type}",
            f"Question: {question}",
        ]
    )
    if correction is not None:
        lines.append(f"Correction: {correction}")
    lines.extend(
        [
            f"Tags: {', '.join(str(tag) for tag in tags)}",
            f"Submitted (UTC): {submitted}",
            f"Group ID: {message.get('group_id') or 'none'}",
            f"Occurrence count: {message['occurrence_count']}",
        ]
    )
    origin = config.get("dashboard_origin")
    if origin:
        lines.append(f"Dashboard: {origin.rstrip('/')}/deliveries?delivery_id={quote(delivery_id)}")
    mail.set_content("\n".join(lines) + "\n")
    mail.add_alternative(
        "<html><body><pre>" + html.escape("\n".join(lines)) + "</pre></body></html>",
        subtype="html",
    )
    return mail


class SmtpAdapter:
    async def send(
        self,
        message: dict[str, Any],
        config: dict[str, Any],
        secrets: dict[str, Any],
        *,
        timeout: int,
    ) -> Any:
        from kebivo.destinations.base import DeliveryResult, failure

        def post() -> DeliveryResult:
            try:
                full = {**config, **secrets, "type": "smtp"}
                validate_config(full, {})
                if (
                    not isinstance(timeout, int)
                    or isinstance(timeout, bool)
                    or not 1 <= timeout <= 30
                ):
                    raise ValueError
                if config.get("username") and not secrets.get("password"):
                    raise ValueError
                private = config.get("allow_private_network") is True
                # Resolve once; every answer must pass, and only those answers are dialed.
                results = socket.getaddrinfo(
                    config["host"], config["port"], type=socket.SOCK_STREAM
                )
                if any(not isinstance(item[4][0], str) for item in results):
                    return failure("permanent", "blocked_destination")
                addresses = list(
                    dict.fromkeys(str(item[4][0]).split("%", 1)[0] for item in results)
                )
                if not addresses or any(
                    not allowed_address(ipaddress.ip_address(value), private)
                    for value in addresses
                ):
                    return failure("permanent", "blocked_destination")
                mail = _mail(message, config)
                tls = config.get("tls_mode", "starttls")
                context = ssl.create_default_context() if tls == "implicit_tls" else None
                with _PinnedSMTP(
                    addresses, config["host"], config["port"], timeout=timeout, context=context
                ) as client:
                    if tls == "starttls":
                        code, _ = client.ehlo()
                        if 400 <= code < 500:
                            return failure("retryable", "smtp_transient", http_status=code)
                        if code >= 500:
                            return failure("permanent", "smtp_permanent", http_status=code)
                        if not client.has_extn("starttls"):
                            return failure("permanent", "tls_unavailable")
                        client.starttls(context=ssl.create_default_context())
                        code, _ = client.ehlo()
                        if 400 <= code < 500:
                            return failure("retryable", "smtp_transient", http_status=code)
                        if code >= 500:
                            return failure("permanent", "smtp_permanent", http_status=code)
                    if config.get("username"):
                        client.login(config["username"], secrets["password"])
                    client.send_message(mail)
                return DeliveryResult("success")
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError) as exc:
                auth_code = (
                    exc.smtp_code if isinstance(exc, smtplib.SMTPResponseException) else None
                )
                return failure("permanent", "smtp_auth", http_status=auth_code)
            except smtplib.SMTPRecipientsRefused as exc:
                codes = [code for code, _ in exc.recipients.values()]
                if codes and all(500 <= code < 600 for code in codes):
                    return failure("permanent", "smtp_permanent", http_status=codes[0])
                reply_code = next((item for item in codes if 400 <= item < 500), None)
                return failure("retryable", "smtp_transient", http_status=reply_code)
            except smtplib.SMTPResponseException as exc:
                if exc.smtp_code == 535:
                    return failure("permanent", "smtp_auth", http_status=exc.smtp_code)
                if 400 <= exc.smtp_code < 500:
                    return failure("retryable", "smtp_transient", http_status=exc.smtp_code)
                return failure("permanent", "smtp_permanent", http_status=exc.smtp_code)
            except ssl.SSLError:
                return failure("permanent", "tls")
            except (TimeoutError, smtplib.SMTPServerDisconnected, OSError):
                return failure("retryable", "smtp_transient")
            except (ConfigError, KeyError, TypeError, ValueError, UnicodeError):
                return failure("permanent", "config_invalid")
            except smtplib.SMTPException:
                return failure("retryable", "smtp_transient")

        return await asyncio.to_thread(post)
