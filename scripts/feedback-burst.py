"""Measure feedback write contention against a disposable Kebivo database."""

import argparse
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--database", required=True, help="SQLite file used by the running server")
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--writers", type=int, default=8)
    args = parser.parse_args()
    if args.events < 1 or args.writers < 1:
        parser.error("events and writers must be positive")
    token = os.environ.get("KEBIVO_BURST_TOKEN")
    if not token:
        parser.error("set KEBIVO_BURST_TOKEN to a disposable project token")
    run_id = uuid.uuid4().hex
    prefix = f"burst-{run_id}-"

    def send(index: int) -> tuple[int | str, float]:
        body = json.dumps({"event_type": "knowledge_gap", "question": f"{prefix}{index}"}).encode()
        request = Request(
            f"{args.url.rstrip('/')}/api/v1/feedback",
            body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": f"{prefix}{index}",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=25) as response:
                status: int | str = response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
        except URLError:
            status = "network_error"
        return status, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=args.writers) as pool:
        results = list(pool.map(send, range(args.events)))
    counts = Counter(status for status, _ in results)
    latencies = sorted(duration for _, duration in results)
    with sqlite3.connect(args.database) as db:
        persisted = db.execute(
            "SELECT count(*) FROM feedback_events WHERE question LIKE ?", (f"{prefix}%",)
        ).fetchone()[0]
    summary = {
        "events": args.events,
        "writers": args.writers,
        "responses": {str(code): count for code, count in sorted(counts.items(), key=lambda x: str(x[0]))},
        "persisted": persisted,
        "p95_seconds": round(latencies[int(0.95 * (len(latencies) - 1))], 3),
        "max_seconds": round(max(latencies), 3),
    }
    print(json.dumps(summary, sort_keys=True))
    if persisted != counts[202] or counts[500]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
