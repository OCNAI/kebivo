"""Validate provider configuration and keep secrets out of public fields."""

import re
from typing import Any

from kebivo.destinations.validation import TOKEN, ConfigError, _webhook_url

ENV_NAME = re.compile(r"^LLM_API_KEY_[A-Z0-9_]{1,64}$")
_FORBIDDEN = {
    "host",
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "upgrade",
    "authorization",
    "api-key",
}


def validate(data: dict[str, Any]) -> None:
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ConfigError("name", "Name must be 1 to 100 characters")
    if data.get("provider_type") not in {"openai_compatible", "azure_openai", "local"}:
        raise ConfigError("provider_type", "Invalid provider type")
    if not isinstance(data.get("allow_private_network"), bool):
        raise ConfigError("allow_private_network", "Must be boolean")
    url = data.get("base_url")
    if not isinstance(url, str) or len(url) > 2048:
        raise ConfigError("base_url", "Invalid URL")
    try:
        _webhook_url(url, data["allow_private_network"])
    except ConfigError:
        raise ConfigError("base_url", "Invalid or disallowed URL") from None
    for field, max_len in (("model", 200), ("deployment", 200), ("api_version", 100)):
        value = data.get(field)
        needed = (field == "model" and data["provider_type"] != "azure_openai") or (
            data["provider_type"] == "azure_openai" and field != "model"
        )
        if needed and (
            not isinstance(value, str)
            or not value
            or len(value) > max_len
            or any(ord(c) < 32 for c in value)
        ):
            raise ConfigError(field, "Required string is invalid")
        if value is not None and (not isinstance(value, str) or len(value) > max_len):
            raise ConfigError(field, "Invalid string")
    if data.get("structured_output") not in {"json_schema", "json_object", "prompt_only"}:
        raise ConfigError("structured_output", "Invalid structured output mode")
    timeout = data.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise ConfigError("timeout_seconds", "Must be 1 to 60")
    if not isinstance(data.get("enabled"), bool):
        raise ConfigError("enabled", "Must be boolean")
    env = data.get("api_key_env")
    if env is not None and (not isinstance(env, str) or not ENV_NAME.fullmatch(env)):
        raise ConfigError("api_key_env", "Invalid environment key name")
    headers = data.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 10:
        raise ConfigError("headers", "At most 10 headers")
    seen: set[str] = set()
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not TOKEN.fullmatch(name)
            or name.lower() in seen
            or name.lower() in _FORBIDDEN
            or name.lower().startswith("proxy-")
        ):
            raise ConfigError("headers", "Invalid header name")
        seen.add(name.lower())
        if value is not None and (
            not isinstance(value, str)
            or len(value.encode()) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ConfigError("headers", "Invalid header value")
    key = data.get("api_key")
    if key is not None and (not isinstance(key, str) or not key or len(key.encode()) > 4096):
        raise ConfigError("api_key", "Invalid API key")
