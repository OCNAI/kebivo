"""Validated environment-backed settings for the foundation phase."""

import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_DEVELOPMENT_ENCRYPTION_KEY = secrets.token_hex(32)

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "project.json"


class SettingsError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    display_name: str
    environment: str
    secret_key: str | None
    database_path: str
    database_url: str | None = None
    allowed_origins: tuple[str, ...] = ()
    public_url: str | None = None
    trusted_proxy_ips: tuple[str, ...] = ()
    feedback_rate_limit_per_token: int = 120
    feedback_rate_limit_per_ip: int = 300
    idempotency_window_hours: int = 24
    mcp_enabled: bool = True
    mcp_allowed_hosts: tuple[str, ...] = ()
    encryption_key: str = _DEVELOPMENT_ENCRYPTION_KEY
    encryption_key_previous: str | None = None
    webhook_user_agent: str = "Kebivo-webhook/1"
    worker_enabled: bool = True
    worker_concurrency: int = 4
    llm_max_concurrency: int = 2
    worker_poll_interval_seconds: int = 1
    worker_shutdown_grace_seconds: int = 20
    delivery_retry_schedule_seconds: tuple[int, ...] = (30, 120, 600, 1800, 7200)
    delivery_retry_after_max_seconds: int = 7200
    feedback_retention_days: int = 180
    delivery_log_retention_days: int = 30
    audit_retention_days: int = 90

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Load and validate settings. Secrets are never included in errors."""
        values = os.environ if environ is None else environ
        try:
            with PROJECT_CONFIG.open(encoding="utf-8") as config_file:
                config = json.load(config_file)
            configured_name = config["display_name"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SettingsError("config/project.json must define a display_name") from exc

        display_name = values.get("DISPLAY_NAME", configured_name)
        if not isinstance(display_name, str) or not display_name.strip():
            raise SettingsError("DISPLAY_NAME must be a non-empty string")

        environment = values.get("APP_ENV", "development").lower()
        if environment not in {"development", "test", "production"}:
            raise SettingsError("APP_ENV must be development, test, or production")

        secret_key = values.get("SECRET_KEY")
        if environment == "production":
            if not secret_key or len(secret_key) < 32:
                raise SettingsError("SECRET_KEY must contain at least 32 characters in production")
            if secret_key.lower() in {"changeme", "change-me", "secret", "development"}:
                raise SettingsError("SECRET_KEY must not be a known placeholder in production")

        def encryption_key(name: str, required: bool = False) -> str | None:
            value = values.get(name)
            if not value:
                if required:
                    raise SettingsError(f"{name} is required in production")
                return None
            if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise SettingsError(f"{name} must be 64 hex characters")
            if value.lower() in {"0" * 64, "f" * 64, "a" * 64, "1" * 64} or value == secret_key:
                raise SettingsError(f"{name} must not be a placeholder or SECRET_KEY")
            return value.lower()

        current_key = encryption_key("ENCRYPTION_KEY", environment == "production")
        previous_key = encryption_key("ENCRYPTION_KEY_PREVIOUS")
        if current_key is None:
            current_key = _DEVELOPMENT_ENCRYPTION_KEY
            logging.getLogger(__name__).warning("Using an ephemeral development encryption key")
        if previous_key == current_key:
            raise SettingsError("ENCRYPTION_KEY_PREVIOUS must differ from ENCRYPTION_KEY")

        database_url = values.get("DATABASE_URL", "sqlite:///./data/app.sqlite3")
        if not database_url.startswith("sqlite:///"):
            raise SettingsError("DATABASE_URL must use SQLite for this application phase")
        database_path = values.get("DATABASE_PATH", database_url.removeprefix("sqlite:///"))
        if not database_path.strip():
            raise SettingsError("DATABASE_PATH must be non-empty")
        if environment == "production" and database_path == ":memory:":
            raise SettingsError("DATABASE_PATH must be persistent in production")

        origins_raw = values.get("ALLOWED_ORIGINS", "")
        allowed_origins = tuple(
            dict.fromkeys(
                origin.strip().rstrip("/") for origin in origins_raw.split(",") if origin.strip()
            )
        )
        for origin in allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise SettingsError("ALLOWED_ORIGINS must contain comma-separated HTTP(S) origins")
            if environment == "production" and parsed.scheme == "http":
                try:
                    is_loopback = bool(ipaddress.ip_address(parsed.hostname or "").is_loopback)
                except ValueError:
                    is_loopback = parsed.hostname in {"localhost", "localhost.localdomain"}
                if not is_loopback:
                    raise SettingsError("Production ALLOWED_ORIGINS must use HTTPS except loopback")

        public_url = values.get("PUBLIC_URL", "").rstrip("/") or None
        if public_url:
            try:
                parsed = urlsplit(public_url)
                _ = parsed.port
            except ValueError:
                raise SettingsError("PUBLIC_URL must be an HTTPS origin") from None
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(ord(char) < 32 or ord(char) == 127 for char in public_url)
            ):
                raise SettingsError("PUBLIC_URL must be an HTTPS origin")

        proxy_raw = values.get("TRUSTED_PROXY_IPS", "")
        try:
            trusted_proxy_ips = tuple(
                dict.fromkeys(
                    str(ipaddress.ip_address(value.strip()))
                    for value in proxy_raw.split(",")
                    if value.strip()
                )
            )
        except ValueError as exc:
            raise SettingsError(
                "TRUSTED_PROXY_IPS must contain comma-separated IP addresses"
            ) from exc

        def positive_limit(name: str, default: int) -> int:
            try:
                value = int(values.get(name, str(default)))
            except ValueError as exc:
                raise SettingsError(f"{name} must be a positive integer") from exc
            if value < 1:
                raise SettingsError(f"{name} must be a positive integer")
            return value

        window_hours = positive_limit("IDEMPOTENCY_WINDOW_HOURS", 24)
        if window_hours > 720:
            raise SettingsError("IDEMPOTENCY_WINDOW_HOURS must be at most 720")

        enabled_raw = values.get("MCP_ENABLED", "true").lower()
        if enabled_raw not in {"true", "false"}:
            raise SettingsError("MCP_ENABLED must be true or false")
        hosts = tuple(
            dict.fromkeys(
                h.strip() for h in values.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
            )
        )
        for host in hosts:
            if not re.fullmatch(
                r"(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(?::(?:[1-9][0-9]{0,4}|\*))?", host
            ):
                raise SettingsError("MCP_ALLOWED_HOSTS must contain valid host[:port] entries")
            port = host.rsplit(":", 1)[-1]
            if port.isdecimal() and int(port) > 65535:
                raise SettingsError("MCP_ALLOWED_HOSTS contains an invalid port")

        def bounded(name: str, default: int, maximum: int) -> int:
            result = positive_limit(name, default)
            if result > maximum:
                raise SettingsError(f"{name} must be between 1 and {maximum}")
            return result

        def retention_days(name: str, default: int) -> int:
            raw = values.get(name, str(default))
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
                raise SettingsError(f"{name} must be between 0 and 36500")
            result = int(raw)
            if result > 36500:
                raise SettingsError(f"{name} must be between 0 and 36500")
            return result

        worker_raw = values.get("WORKER_ENABLED", "true").lower()
        if worker_raw not in {"true", "false"}:
            raise SettingsError("WORKER_ENABLED must be true or false")
        schedule_raw = values.get("DELIVERY_RETRY_SCHEDULE_SECONDS", "30,120,600,1800,7200")
        if not re.fullmatch(r"[0-9]+(?:,[0-9]+){0,4}", schedule_raw):
            raise SettingsError(
                "DELIVERY_RETRY_SCHEDULE_SECONDS must contain 1-5 positive integers"
            )
        schedule = tuple(int(part) for part in schedule_raw.split(","))
        if any(part < 1 for part in schedule):
            raise SettingsError(
                "DELIVERY_RETRY_SCHEDULE_SECONDS must contain 1-5 positive integers"
            )

        return cls(
            display_name=display_name,
            environment=environment,
            secret_key=secret_key,
            database_path=database_path,
            database_url=database_url,
            allowed_origins=allowed_origins,
            public_url=public_url,
            trusted_proxy_ips=trusted_proxy_ips,
            feedback_rate_limit_per_token=positive_limit("FEEDBACK_RATE_LIMIT_PER_TOKEN", 120),
            feedback_rate_limit_per_ip=positive_limit("FEEDBACK_RATE_LIMIT_PER_IP", 300),
            idempotency_window_hours=window_hours,
            mcp_enabled=enabled_raw == "true",
            mcp_allowed_hosts=hosts,
            encryption_key=current_key,
            encryption_key_previous=previous_key,
            worker_enabled=worker_raw == "true",
            worker_concurrency=bounded("WORKER_CONCURRENCY", 4, 32),
            llm_max_concurrency=bounded("LLM_MAX_CONCURRENCY", 2, 8),
            worker_poll_interval_seconds=bounded("WORKER_POLL_INTERVAL_SECONDS", 1, 60),
            worker_shutdown_grace_seconds=bounded("WORKER_SHUTDOWN_GRACE_SECONDS", 20, 120),
            delivery_retry_schedule_seconds=schedule,
            delivery_retry_after_max_seconds=positive_limit(
                "DELIVERY_RETRY_AFTER_MAX_SECONDS", 7200
            ),
            feedback_retention_days=retention_days("FEEDBACK_RETENTION_DAYS", 180),
            delivery_log_retention_days=retention_days("DELIVERY_LOG_RETENTION_DAYS", 30),
            audit_retention_days=retention_days("AUDIT_RETENTION_DAYS", 90),
        )
