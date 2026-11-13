---
title: Report from your own app
description: Send an unanswered chatbot question to Kebivo from your application fallback.
---

# Report from your own app

Your app knows when it cannot answer. In this example, `answer is None` is the no-answer condition, after your own retrieval and fallback logic have run. An empty retrieval result is one possible reason to reach that condition; a low-confidence answer or a failed lookup may be another. Pass the user's original question to Kebivo, not a rewritten search query.

Create a project token with `feedback:gap` in **Projects** ([quickstart](quickstart.md)), then set `KEBIVO_TOKEN` in your server environment. Do not put it in browser code. Save this as `report_gap.py`:

```python
import json
import os
import urllib.request
import uuid


def report_if_unanswered(question: str, answer: str | None, turn_id: str):
    if answer is not None:
        return None

    body = json.dumps({"event_type": "knowledge_gap", "question": question}).encode()
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kebivo-report:{turn_id}"))
    request = urllib.request.Request(
        os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/")
        + "/api/v1/feedback",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['KEBIVO_TOKEN']}",
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


if __name__ == "__main__":
    result = report_if_unanswered("Do you ship to Canada?", None, "demo-turn-1")
    print(result)
```

Run `python3 report_gap.py` from your server shell. It prints a result with `status: accepted` and an `event_id`. Open **Inbox** and find **Do you ship to Canada?** in your project. The report appears as a group row; repeat the command with the same `turn_id` to replay the same event without adding an occurrence. Use a new `turn_id` for a new user turn. Keep that ID stable across retries, including after a timeout.

The function raises on HTTP or network errors. In your chatbot, catch and log those errors so reporting does not interrupt the answer. Retry temporary failures with the same `turn_id` (see [REST retries](rest-api.md#retries-and-rate-limits)). A model's MCP tool call is not guaranteed; this app-controlled fallback attempts to report every case that meets the stated no-answer condition.
