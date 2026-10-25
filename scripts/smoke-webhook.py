"""Private-network webhook receiver used by container smoke acceptance."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
requests: list[dict[str, object]] = []
failures = int(os.environ.get("FAIL_FIRST", "0"))
hold_seconds = float(os.environ.get("HOLD_SECONDS", "0"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        with lock:
            content = json.dumps(requests).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_DELETE(self) -> None:  # noqa: N802
        global failures, hold_seconds
        with lock:
            requests.clear()
            failures = int(self.headers.get("X-Fail-First", "0"))
            hold_seconds = float(self.headers.get("X-Hold-Seconds", "0"))
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        global failures
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        item = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body.decode("utf-8", errors="replace"),
            "received_at": time.time(),
        }
        with lock:
            requests.append(item)
            fail = failures > 0
            if fail:
                failures -= 1
            delay = hold_seconds
        if delay:
            time.sleep(delay)
        self.send_response(500 if fail else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
