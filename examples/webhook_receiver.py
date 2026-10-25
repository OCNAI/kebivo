"""Receive Kebivo webhooks, verify the signature and check the payload schema.

Usage:
    WEBHOOK_SECRET=whsec_... python examples/webhook_receiver.py --port 9000

Standard library only. For every request the receiver checks the Standard
Webhooks signature over the raw body (constant-time compare, timestamp within
five minutes), then validates the JSON against docs/webhook-payload.schema.json
and ignores repeats of a webhook-id it already accepted. It answers 204 on
success, 401 for a bad signature and 400 for a payload that does not match the
schema, and prints one JSON line per accepted delivery.
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
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOLERANCE_SECONDS = 300
DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "webhook-payload.schema.json"
TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
    "number": (int, float),
    "integer": int,
}


def verify_signature(headers: Any, body: bytes, secret: str, now: float | None = None) -> bool:
    webhook_id = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    if not webhook_id or not re.fullmatch(r"\d{1,12}", stamp):
        return False
    if abs((now if now is not None else time.time()) - int(stamp)) > TOLERANCE_SECONDS:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    signed = f"{webhook_id}.{stamp}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # The header may carry several space-separated signatures during rotation.
    return any(
        hmac.compare_digest(expected, candidate)
        for candidate in headers.get("webhook-signature", "").split()
    )


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Check the JSON Schema keywords the Kebivo payload schema uses."""
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not one of {schema['enum']}")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        matches = [
            name
            for name in names
            if isinstance(value, TYPES[name])
            and not (name in {"integer", "number"} and isinstance(value, bool))
        ]
        if not matches:
            return errors + [f"{path}: expected type {schema['type']}"]
    if isinstance(value, str) and schema.get("format") == "uuid":
        try:
            uuid.UUID(value)
        except ValueError:
            errors.append(f"{path}: not a UUID")
    if isinstance(value, str) and schema.get("format") == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{path}: not a date-time")
    if isinstance(value, int | float) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: below {schema['minimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        errors += [f"{path}.{key}: required" for key in missing]
        for key, item in value.items():
            if key in properties:
                errors += schema_errors(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: not allowed")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{index}]")
    return errors


class Receiver(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], secret: str, schema: dict[str, Any]) -> None:
        super().__init__(address, Handler)
        self.secret = secret
        self.schema = schema
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        self.accepted = 0


class Handler(BaseHTTPRequestHandler):
    server: Receiver

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
        errors = schema_errors(payload, self.server.schema)
        if errors:
            print(json.dumps({"rejected": headers["webhook-id"], "errors": errors}), flush=True)
            self._answer(400)
            return
        with self.server.lock:
            repeat = headers["webhook-id"] in self.server.seen
            self.server.seen.add(headers["webhook-id"])
        self._answer(204)
        if not repeat:
            # Hand the event to your own queue here; answer quickly.
            print(
                json.dumps(
                    {
                        "webhook_id": headers["webhook-id"],
                        "event_id": payload["event_id"],
                        "event_type": payload["event_type"],
                        "question": payload["feedback"]["question"],
                        "signature": "valid",
                        "schema": "valid",
                    }
                ),
                flush=True,
            )
            with self.server.lock:
                self.server.accepted += 1

    def _answer(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--exit-after", type=int, help="Stop after this many accepted deliveries")
    args = parser.parse_args()
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret.startswith("whsec_"):
        print("Set WEBHOOK_SECRET to the whsec_ signing secret", file=sys.stderr)
        raise SystemExit(2)
    server = Receiver((args.host, args.port), secret, json.loads(args.schema.read_text()))
    server.timeout = 0.5
    print(json.dumps({"listening": f"{args.host}:{args.port}"}), flush=True)
    with server:
        while args.exit_after is None or server.accepted < args.exit_after:
            server.handle_request()


if __name__ == "__main__":
    main()
