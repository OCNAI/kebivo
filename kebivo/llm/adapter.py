"""Single bounded chat-completion adapter for configured providers."""

import json
import math
import os
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from kebivo.models import LlmProviderConfig
from kebivo.security.outbound import BlockedDestination, ResponseTooLarge, post_pinned

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same_issue": {"type": "boolean"},
        "candidate": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_issue", "candidate", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LlmResult:
    answer: dict[str, Any] | None = None
    error_category: str | None = None
    http_status: int | None = None
    latency_ms: int = 0


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]] | None = None
    error_category: str | None = None
    http_status: int | None = None
    latency_ms: int = 0


def validate_embeddings(value: Any, count: int) -> list[list[float]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return None
    data = value["data"]
    if len(data) != count:
        return None
    ordered: list[list[float] | None] = [None] * count
    dims: int | None = None
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            return None
        index = item.get("index", position)
        vector = item.get("embedding")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < count
            or ordered[index] is not None
            or not isinstance(vector, list)
            or not 1 <= len(vector) <= 8192
            or (dims is not None and len(vector) != dims)
            or any(
                isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x)
                for x in vector
            )
        ):
            return None
        dims = len(vector)
        ordered[index] = vector
    return (
        [item for item in ordered if item is not None]
        if all(item is not None for item in ordered)
        else None
    )


def validate_answer(value: Any, offered: set[str]) -> str | None:
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        return "invalid_response"
    same, candidate, confidence, reason = (value[k] for k in SCHEMA["required"])
    if (
        not isinstance(same, bool)
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 <= confidence <= 1
        or not isinstance(reason, str)
        or len(reason) > 500
        or (candidate is not None and not isinstance(candidate, str))
        or (same and candidate is None)
        or (not same and candidate is not None)
    ):
        return "invalid_response"
    if candidate is not None and candidate not in offered:
        return "invalid_candidate"
    return None


def _url(provider: LlmProviderConfig) -> str:
    base = provider.base_url.rstrip("/")
    if provider.provider_type == "azure_openai":
        deployment = quote(provider.deployment or "", safe="")
        version = quote(provider.api_version or "", safe="")
        return f"{base}/openai/deployments/{deployment}/chat/completions?api-version={version}"
    return base + "/chat/completions"


def _error(exc: Exception) -> str:
    if isinstance(exc, BlockedDestination):
        return "blocked_address"
    if isinstance(exc, ResponseTooLarge):
        return "too_large"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, ssl.SSLError):
                return "tls"
            cause = cause.__cause__
    return "connect"


async def embed(
    provider: LlmProviderConfig,
    secrets: dict[str, str],
    embedding_model: str,
    inputs: list[str],
) -> EmbedResult:
    if provider.provider_type == "azure_openai":
        raise ValueError("Azure OpenAI embeddings are unsupported in v1")
    if provider.provider_type not in {"openai_compatible", "local"}:
        raise ValueError("Unsupported embedding provider")
    if not 1 <= len(inputs) <= 16:
        raise ValueError("Invalid embedding batch")
    started = time.monotonic()
    headers = {"Content-Type": "application/json"}
    for name in json.loads(provider.headers_json):
        headers[name] = secrets[f"header:{name}"]
    key = secrets.get("api_key") or (
        os.environ.get(provider.api_key_env) if provider.api_key_env else None
    )
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": embedding_model, "input": [value.lower()[:2000] for value in inputs]}
    try:
        status, _, body = await post_pinned(
            provider.base_url.rstrip("/") + "/embeddings",
            headers,
            json.dumps(payload, separators=(",", ":")).encode(),
            private=provider.allow_private_network,
            timeout=provider.timeout_seconds,
            max_response_bytes=4 * 1024 * 1024,
            return_body=True,
        )
    except ResponseTooLarge:
        return EmbedResult(
            error_category="invalid_response", latency_ms=int((time.monotonic() - started) * 1000)
        )
    except (BlockedDestination, TimeoutError, httpx.HTTPError, socket.gaierror, OSError) as exc:
        return EmbedResult(
            error_category=_error(exc), latency_ms=int((time.monotonic() - started) * 1000)
        )
    elapsed = int((time.monotonic() - started) * 1000)
    if status == 429:
        return EmbedResult(error_category="http_429", http_status=status, latency_ms=elapsed)
    if status >= 500:
        return EmbedResult(error_category="http_5xx", http_status=status, latency_ms=elapsed)
    if not 200 <= status < 300:
        return EmbedResult(error_category="http_4xx", http_status=status, latency_ms=elapsed)
    try:
        vectors = validate_embeddings(json.loads(body), len(inputs))
    except (ValueError, TypeError):
        vectors = None
    if vectors is None:
        return EmbedResult(
            error_category="invalid_response", http_status=status, latency_ms=elapsed
        )
    return EmbedResult(vectors=vectors, http_status=status, latency_ms=elapsed)


async def complete(
    provider: LlmProviderConfig, secrets: dict[str, str], system: str, user: str, offered: set[str]
) -> LlmResult:
    started = time.monotonic()
    headers = {"Content-Type": "application/json"}
    for name in json.loads(provider.headers_json):
        headers[name] = secrets[f"header:{name}"]
    key = secrets.get("api_key") or (
        os.environ.get(provider.api_key_env) if provider.api_key_env else None
    )
    if key:
        if provider.provider_type == "azure_openai":
            headers["api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
    payload: dict[str, Any] = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 300,
    }
    if provider.model is not None:
        payload["model"] = provider.model
    if provider.structured_output == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "same_issue",
                "strict": True,
                "schema": SCHEMA,
            },
        }
    elif provider.structured_output == "json_object":
        payload["response_format"] = {"type": "json_object"}
    try:
        response = await post_pinned(
            _url(provider),
            headers,
            json.dumps(payload, separators=(",", ":")).encode(),
            private=provider.allow_private_network,
            timeout=provider.timeout_seconds,
            max_response_bytes=256 * 1024,
            return_body=True,
        )
        status, _, body = response
    except (
        BlockedDestination,
        ResponseTooLarge,
        TimeoutError,
        httpx.HTTPError,
        socket.gaierror,
        OSError,
    ) as exc:
        return LlmResult(
            error_category=_error(exc), latency_ms=int((time.monotonic() - started) * 1000)
        )
    elapsed = int((time.monotonic() - started) * 1000)
    if status == 429:
        return LlmResult(error_category="http_429", http_status=status, latency_ms=elapsed)
    if status >= 500:
        return LlmResult(error_category="http_5xx", http_status=status, latency_ms=elapsed)
    if not 200 <= status < 300:
        return LlmResult(error_category="http_4xx", http_status=status, latency_ms=elapsed)
    try:
        envelope = json.loads(body)
        content = envelope["choices"][0]["message"]["content"]
        answer = json.loads(content) if isinstance(content, str) else content
    except (ValueError, KeyError, IndexError, TypeError):
        return LlmResult(error_category="invalid_json", http_status=status, latency_ms=elapsed)
    failure = validate_answer(answer, offered)
    if failure:
        return LlmResult(error_category=failure, http_status=status, latency_ms=elapsed)
    return LlmResult(answer=answer, http_status=status, latency_ms=elapsed)
