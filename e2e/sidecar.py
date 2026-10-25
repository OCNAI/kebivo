"""Test doubles for the acceptance suite, run as the e2e runner's main process.

Everything listens inside the e2e network, so the suite also works offline:
- webhook receiver on :8080 (scripts/smoke-webhook.py, GET lists, DELETE resets);
- SMTP sink on :2525 (aiosmtpd); each message is written to $E2E_STATE/mail/,
  and while $E2E_STATE/smtp-down exists every message gets a 451 answer;
- scripted LLM server on :8765 (scripts/fake_llm_server.py);
- DNS sink on :53/udp. The app container uses it as its upstream resolver, so
  every query that Docker's embedded DNS cannot answer from the e2e network
  (that is, any external name) is recorded in $E2E_STATE/dns.jsonl and refused.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from aiosmtpd.controller import Controller

STATE = Path(os.environ.get("E2E_STATE", "/tmp/e2e"))
ROOT = Path(__file__).resolve().parent.parent


class MailSink:
    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        if (STATE / "smtp-down").exists():
            return "451 4.3.0 Sink is down for this test"
        with self.lock:
            self.count += 1
            number = self.count
        record = {
            "mail_from": envelope.mail_from,
            "rcpt_tos": envelope.rcpt_tos,
            "data": envelope.content.decode("utf-8", errors="replace"),
            "received_at": time.time(),
        }
        (STATE / "mail" / f"{number:06d}.json").write_text(json.dumps(record))
        return "250 OK"


def dns_sink(stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    sock.settimeout(0.5)
    log = STATE / "dns.jsonl"
    while not stop.is_set():
        try:
            packet, peer = sock.recvfrom(2048)
        except TimeoutError:
            continue
        name, offset = [], 12
        try:
            while packet[offset]:
                length = packet[offset]
                name.append(packet[offset + 1 : offset + 1 + length].decode("ascii", "replace"))
                offset += 1 + length
        except IndexError:
            name = ["<malformed>"]
        with log.open("a") as stream:
            stream.write(json.dumps({"name": ".".join(name), "peer": peer[0]}) + "\n")
        # Answer REFUSED with the question echoed, so resolvers fail fast.
        flags = bytes([0x80 | (packet[2] & 0x01), 0x05]) if len(packet) > 3 else b"\x80\x05"
        sock.sendto(packet[:2] + flags + packet[4:], peer)


def main() -> None:
    (STATE / "mail").mkdir(parents=True, exist_ok=True)
    (STATE / "state").mkdir(parents=True, exist_ok=True)
    (STATE / "dns.jsonl").touch()
    children = [
        subprocess.Popen([sys.executable, str(ROOT / "scripts" / "smoke-webhook.py")]),
        subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "fake_llm_server.py"), "--host", "0.0.0.0"]
        ),
    ]
    smtp = Controller(MailSink(), hostname="0.0.0.0", port=2525)
    smtp.start()
    stop = threading.Event()
    threading.Thread(target=dns_sink, args=(stop,), daemon=True).start()
    (STATE / "ready").write_text("ok")
    print("e2e sidecar ready", flush=True)

    def shutdown(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while not stop.is_set():
        if any(child.poll() is not None for child in children):
            print("a sidecar child exited", file=sys.stderr, flush=True)
            break
        time.sleep(0.5)
    smtp.stop()
    for child in children:
        child.terminate()


if __name__ == "__main__":
    main()
