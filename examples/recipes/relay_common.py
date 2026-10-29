"""Shared part of the webhook relay recipes: verify, deduplicate, forward.

Each recipe imports this module and supplies one
function that turns a verified Kebivo payload into an outbound HTTP request.
Standard library only. Copy this file next to the recipe you use.

How the relay answers Kebivo, so Kebivo's delivery retries do the right thing:

- 204 when the target service accepted the message, or the webhook-id was
  already forwarded (Kebivo delivers at least once);
- 401 for a bad signature, 400 for a body that is not JSON (permanent);
- 429 with the target's Retry-After when the target rate-limits (retried);
- 502 when the target is unreachable or answers 5xx (retried);
- 424 when the target answers another 4xx or reports an error in its answer,
  such as a wrong API key or project (permanent: fix the relay settings, then
  retry the delivery in Kebivo).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOLERANCE_SECONDS = 300
REMEMBERED_IDS = 10_000


@dataclass
class Outbound:
    """One HTTP request to the target service."""

    url: str
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    # Optional check of a 2xx response body, for APIs that report errors with 200.
    accepted: Callable[[bytes], bool] | None = None


Mapper = Callable[[dict[str, Any]], Outbound]


def verify_signature(headers: dict[str, str], body: bytes, secret: str) -> bool:
    """Standard Webhooks check, the same as examples/webhook_receiver.py."""
    webhook_id = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    if not webhook_id or not re.fullmatch(r"\d{1,12}", stamp):
        return False
    if abs(time.time() - int(stamp)) > TOLERANCE_SECONDS:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    signed = f"{webhook_id}.{stamp}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        hmac.compare_digest(expected, candidate)
        for candidate in headers.get("webhook-signature", "").split()
    )


def title(payload: dict[str, Any], limit: int = 120) -> str:
    """A one-line issue title from the reported question."""
    prefix = "[Kebivo test] " if payload["test"] else ""
    kind = "Correction" if payload["event_type"] == "knowledge_correction" else "Knowledge gap"
    question = " ".join(payload["feedback"]["question"].split())
    text = f"{prefix}{kind}: {question}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def inbox_link(payload: dict[str, Any]) -> str | None:
    """Link to the group in the Kebivo admin UI when KEBIVO_URL is set."""
    origin = os.environ.get("KEBIVO_URL", "").rstrip("/")
    if not origin:
        return None
    if payload["group_id"]:
        return f"{origin}/inbox/groups/{payload['group_id']}"
    return f"{origin}/inbox"


def forward(outbound: Outbound, timeout: float) -> tuple[int, dict[str, str]]:
    """Send the request; return the status and headers to answer Kebivo with."""
    request = urllib.request.Request(
        outbound.url,
        data=json.dumps(outbound.body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **outbound.headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = response.read(65_536)
    except urllib.error.HTTPError as error:
        detail = error.read()[:500].decode(errors="replace")
        print(json.dumps({"target_status": error.code, "target_body": detail}), file=sys.stderr)
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "")
            return 429, {"Retry-After": retry_after} if retry_after.isdigit() else {}
        return (502 if error.code >= 500 else 424), {}
    except (urllib.error.URLError, TimeoutError) as error:
        print(json.dumps({"target_error": str(error)}), file=sys.stderr)
        return 502, {}
    if outbound.accepted is not None and not outbound.accepted(answer):
        detail = answer[:500].decode(errors="replace")
        print(json.dumps({"target_rejected": detail}), file=sys.stderr)
        return 424, {}
    return 204, {}


class Relay(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], secret: str, mapper: Mapper) -> None:
        super().__init__(address, Handler)
        self.secret = secret
        self.mapper = mapper
        self.timeout_seconds = 8.0
        self.forwarded: OrderedDict[str, None] = OrderedDict()
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: Relay

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = {key.lower(): value for key, value in self.headers.items()}
        if not verify_signature(headers, body, self.server.secret):
            self._answer(401)
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._answer(400)
            return
        webhook_id = headers["webhook-id"]
        with self.server.lock:
            if webhook_id in self.server.forwarded:
                self._answer(204)
                return
        status, extra = forward(self.server.mapper(payload), self.server.timeout_seconds)
        if status == 204:
            with self.server.lock:
                self.server.forwarded[webhook_id] = None
                while len(self.server.forwarded) > REMEMBERED_IDS:
                    self.server.forwarded.popitem(last=False)
        print(
            json.dumps(
                {"webhook_id": webhook_id, "event_id": payload.get("event_id"), "status": status}
            ),
            flush=True,
        )
        self._answer(status, extra)

    def _answer(self, status: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def serve(description: str, mapper: Mapper, required_env: list[str]) -> None:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    secret = os.environ.get("WEBHOOK_SECRET", "")
    missing = [name for name in required_env if not os.environ.get(name)]
    if not secret.startswith("whsec_") or missing:
        print(f"Set WEBHOOK_SECRET (whsec_...) and {', '.join(required_env)}", file=sys.stderr)
        raise SystemExit(2)
    relay = Relay((args.host, args.port), secret, mapper)
    print(json.dumps({"listening": f"{args.host}:{args.port}"}), flush=True)
    with relay:
        relay.serve_forever()
