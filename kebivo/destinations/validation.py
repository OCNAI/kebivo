"""Destination configuration validation and secret separation."""

import ipaddress
import re
from email.headerregistry import Address
from typing import Any
from urllib.parse import urlsplit

from kebivo.security.outbound import allowed_address

# Without allow_private_network, SMTP may only use the standard submission and relay ports.
PUBLIC_SMTP_PORTS = frozenset({25, 465, 587, 2525})

TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
FORBIDDEN_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-type",
    "authorization",
}


class ConfigError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


def _string(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(field, "Must be a non-empty string")
    return value


def _timeout(value: Any, default: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
        raise ConfigError("timeout_seconds", "Must be an integer from 1 to 30")
    return int(value)


def _header_value(value: Any, field: str) -> str:
    result = _string(value, field)
    assert result is not None
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ConfigError(field, "Header value contains a control character")
    return result


def _webhook_url(value: Any, allow_private_network: bool) -> str:
    url = _string(value, "url")
    assert url is not None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
        if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_private_network):
            raise ValueError
        host_idna = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not host_idna or host_idna == "localhost" or host_idna.endswith(".localhost"):
            if not allow_private_network:
                raise ValueError
        try:
            address = ipaddress.ip_address(host_idna.strip("[]"))
        except ValueError:
            pass
        else:
            if not allowed_address(address, allow_private_network):
                raise ValueError
        _ = parsed.port
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ConfigError("url", "Invalid or disallowed webhook URL") from None
    return url


def _email(value: Any, field: str) -> str:
    address = _string(value, field)
    assert address is not None
    try:
        parsed = Address(addr_spec=address)
        if parsed.addr_spec != address or not parsed.username or not parsed.domain:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError(field, "Invalid email address") from None
    return address


def validate_config(
    config: dict[str, Any], secrets: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate one type and return public configuration plus plaintext secrets for encryption.

    The caller passes ``type`` and ``allow_private_network`` in config. Secret values may be
    supplied in either mapping; fields in ``secrets`` take precedence.
    """
    if not isinstance(config, dict) or not isinstance(secrets, dict):
        raise ConfigError("config", "Must be an object")
    kind = config.get("type")
    private = config.get("allow_private_network", False)
    if not isinstance(private, bool):
        raise ConfigError("allow_private_network", "Must be a boolean")
    supplied = {**config, **secrets}
    public: dict[str, Any] = {}
    hidden: dict[str, str] = {}
    if kind == "webhook":
        url = _webhook_url(supplied.get("url"), private)
        hidden["url"] = url
        auth = supplied.get("auth", {"type": "none"})
        if not isinstance(auth, dict) or auth.get("type") not in {"none", "bearer", "header"}:
            raise ConfigError("auth", "Must be none, bearer or header")
        auth_type = auth["type"]
        public["auth"] = {"type": auth_type}
        if auth_type == "bearer":
            token = _header_value(auth.get("token", supplied.get("token")), "auth.token")
            hidden["token"] = token
        if auth_type == "header":
            name = _string(auth.get("name"), "auth.name")
            assert name is not None
            if (
                not TOKEN.fullmatch(name)
                or name.lower() in FORBIDDEN_HEADERS
                or name.lower().startswith("webhook-")
            ):
                raise ConfigError("auth.name", "Invalid header name")
            public["auth"]["name"] = name
            value = _header_value(auth.get("value", supplied.get("header_value")), "auth.value")
            hidden["header_value"] = value
        signing = supplied.get("signing", False)
        if not isinstance(signing, bool):
            raise ConfigError("signing", "Must be a boolean")
        public["signing"] = signing
        headers = supplied.get("custom_headers", {})
        if not isinstance(headers, dict) or len(headers) > 10:
            raise ConfigError("custom_headers", "Must be an object with at most 10 headers")
        public["custom_headers"] = list(headers)
        for name, value in headers.items():
            if (
                not isinstance(name, str)
                or not TOKEN.fullmatch(name)
                or name.lower() in FORBIDDEN_HEADERS
                or name.lower().startswith("webhook-")
            ):
                raise ConfigError("custom_headers", "Invalid header name")
            hidden[f"custom_headers.{name}"] = _header_value(value, f"custom_headers.{name}")
        public["timeout_seconds"] = _timeout(supplied.get("timeout_seconds"), 10)
    elif kind == "slack":
        raw_url = supplied.get("url")
        url = _webhook_url(raw_url.strip() if isinstance(raw_url, str) else raw_url, False)
        parsed = urlsplit(url)
        if (
            parsed.hostname != "hooks.slack.com"
            or parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or not parsed.path.startswith("/services/")
            or not parsed.path.removeprefix("/services/").strip("/")
            or parsed.query
        ):
            raise ConfigError("url", "Must be a Slack incoming-webhook URL")
        if private:
            raise ConfigError("allow_private_network", "Slack does not allow private networks")
        hidden["url"] = url
        public["timeout_seconds"] = _timeout(supplied.get("timeout_seconds"), 10)
    elif kind == "smtp":
        host = _string(supplied.get("host"), "host")
        assert host is not None
        if any(char.isspace() for char in host) or "://" in host:
            raise ConfigError("host", "Invalid SMTP host")
        try:
            public["host"] = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ConfigError("host", "Invalid SMTP host") from None
        port = supplied.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError("port", "Must be an integer from 1 to 65535")
        public["port"] = port
        if not private and port not in PUBLIC_SMTP_PORTS:
            raise ConfigError("port", "Use port 25, 465, 587 or 2525, or allow private network")
        tls = supplied.get("tls_mode", "starttls")
        if tls not in {"starttls", "implicit_tls", "none"}:
            raise ConfigError("tls_mode", "Invalid TLS mode")
        if tls == "none" and not private:
            raise ConfigError("tls_mode", "TLS None requires allow private network")
        public["tls_mode"] = tls
        username = supplied.get("username")
        if username is not None:
            if tls == "none":
                raise ConfigError("username", "Credentials require TLS")
            public["username"] = _string(username, "username")
        password = supplied.get("password")
        if password is not None:
            if tls == "none":
                raise ConfigError("password", "Password requires TLS")
            secret = _string(password, "password")
            assert secret is not None
            hidden["password"] = secret
        public["from"] = _email(supplied.get("from"), "from")
        recipients = supplied.get("to")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 20:
            raise ConfigError("to", "Must contain 1 to 20 email addresses")
        public["to"] = [_email(item, f"to[{index}]") for index, item in enumerate(recipients)]
        public["timeout_seconds"] = _timeout(supplied.get("timeout_seconds"), 15)
    else:
        raise ConfigError("type", "Must be webhook, slack or smtp")
    return public, hidden
