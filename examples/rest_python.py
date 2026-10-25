"""Submit one knowledge gap to Kebivo over REST with the Python standard library.

Usage:
    KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
        python examples/rest_python.py --question "How do I reset my corporate VPN?" --tag it

The script sends one Idempotency-Key for every attempt, so a retry after a
timeout or a 5xx answer cannot create a second event. It retries 429 (after
Retry-After), 5xx and network errors with exponential backoff, and stops at
once on other 4xx answers. Exit codes: 0 accepted, 1 rejected or retries
exhausted, 2 missing configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

RETRYABLE = {429, 500, 502, 503, 504}


def submit(url: str, token: str, body: dict[str, object], key: str, attempts: int) -> int:
    data = json.dumps(body).encode()
    delay = 1.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url.rstrip("/") + "/api/v1/feedback",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        wait = delay
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
                replayed = response.headers.get("Idempotent-Replayed") == "true"
                print(json.dumps({"http_status": response.status, "replayed": replayed, **result}))
                return 0
        except urllib.error.HTTPError as error:
            raw = error.read()
            if error.code not in RETRYABLE or attempt == attempts:
                print(json.dumps({"http_status": error.code, "body": raw.decode(errors="replace")}))
                return 1
            retry_after = error.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = max(wait, float(retry_after))
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                print(json.dumps({"http_status": None, "error": str(error)}))
                return 1
        print(f"attempt {attempt} failed; retrying in {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
        delay *= 2
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--question", default="How do I reset my corporate VPN?")
    parser.add_argument("--tag", action="append", dest="tags", default=None)
    parser.add_argument("--context", help="Optional context the agent had")
    parser.add_argument(
        "--idempotency-key", help="Defaults to $IDEMPOTENCY_KEY, else a new random UUID"
    )
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    url = os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000")
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        print("Set KEBIVO_TOKEN to a project token", file=sys.stderr)
        raise SystemExit(2)
    body: dict[str, object] = {
        "event_type": "knowledge_gap",
        "question": args.question,
        "tags": args.tags if args.tags is not None else ["it"],
    }
    if args.context:
        body["context"] = args.context
    key = args.idempotency_key or os.environ.get("IDEMPOTENCY_KEY") or str(uuid.uuid4())
    raise SystemExit(submit(url, token, body, key, args.attempts))


if __name__ == "__main__":
    main()
