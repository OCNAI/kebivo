#!/usr/bin/env python3
"""Local scripted OpenAI/Azure chat-completion server for tests and QA.

Run: python scripts/fake_llm_server.py --host 127.0.0.1 --port 8765 --scenario valid
Scenarios: valid, wrong_label, malformed, slow, 500, 429, redirect, oversized.
GET /requests returns recorded path, headers and request bodies.
"""

import argparse
import hashlib
import json
import math
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_CONCEPTS = {
    "forgot": "reset",
    "recover": "reset",
    "change": "reset",
    "passcode": "password",
    "login": "password",
    "signin": "password",
    "virtual": "vps",
    "server": "vps",
    "private": "vpn",
    "connect": "access",
    "connection": "access",
    "use": "access",
}
_STOP = {"a", "an", "the", "i", "my", "how", "do", "can", "to", "is", "for", "of", "and", "what"}


def _embedding(value: str) -> list[float]:
    vector = [0.0] * 64
    for token in re.findall(r"[a-z0-9]+", value.lower()):
        if token in _STOP:
            continue
        concept = _CONCEPTS.get(token, token)
        digest = hashlib.sha256(concept.encode()).digest()
        vector[int.from_bytes(digest[:2], "little") % 64] += 1.0
    length = math.sqrt(sum(x * x for x in vector))
    return [x / length for x in vector] if length else vector


class FakeLLMServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], scenario: str = "valid") -> None:
        super().__init__(address, FakeLLMHandler)
        self.scenario = scenario
        self.requests: list[dict[str, Any]] = []


class FakeLLMHandler(BaseHTTPRequestHandler):
    server: FakeLLMServer

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, b'{"status":"ok"}')
            return
        if self.path != "/requests":
            self.send_error(404)
            return
        self._reply(200, json.dumps(self.server.requests).encode())

    def do_PUT(self) -> None:
        if self.path != "/scenario":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            value = json.loads(self.rfile.read(length))
            scenario = value["scenario"]
        except (json.JSONDecodeError, KeyError, TypeError):
            self.send_error(400)
            return
        if scenario not in {
            "valid",
            "no_match",
            "valid_c2",
            "low_confidence",
            "wrong_label",
            "malformed",
            "slow",
            "500",
            "429",
            "redirect",
            "oversized",
        }:
            self.send_error(400)
            return
        self.server.scenario = scenario
        self._reply(200, json.dumps({"scenario": scenario}).encode())

    def do_POST(self) -> None:
        embeddings = self.path.endswith("/embeddings")
        if (
            not embeddings
            and not self.path.endswith("/chat/completions")
            and "/chat/completions?" not in self.path
        ):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(min(length, 1024 * 1024))
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        case = self.server.scenario
        if case == "slow":
            time.sleep(2)
        if case in {"500", "429"}:
            self._reply(int(case), b"{}")
            return
        if case == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if case == "oversized":
            self._reply(200, b"x" * ((4 * 1024 * 1024 if embeddings else 256 * 1024) + 1))
            return
        if case == "malformed":
            self._reply(200, b"this is not JSON")
            return
        if embeddings:
            try:
                values = json.loads(body)["input"]
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self._reply(
                200,
                json.dumps(
                    {
                        "data": [
                            {"index": i, "embedding": _embedding(v)} for i, v in enumerate(values)
                        ]
                    }
                ).encode(),
            )
            return
        candidate = (
            None
            if case == "no_match"
            else "c99"
            if case == "wrong_label"
            else "c2"
            if case == "valid_c2"
            else "c1"
        )
        answer = {
            "same_issue": case != "no_match",
            "candidate": candidate,
            "confidence": 0.8 if case == "low_confidence" else 0.95,
            "reason": "FAKE_REASON_CANARY-D6e8",
        }
        envelope = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        self._reply(200, json.dumps(envelope).encode())

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--scenario",
        choices=[
            "valid",
            "no_match",
            "valid_c2",
            "low_confidence",
            "wrong_label",
            "malformed",
            "slow",
            "500",
            "429",
            "redirect",
            "oversized",
        ],
        default="valid",
    )
    options = parser.parse_args()
    server = FakeLLMServer((options.host, options.port), options.scenario)
    print(
        f"Fake LLM server on {options.host}:{options.port}, scenario={options.scenario}", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
