---
title: Collect AI knowledge gaps with the REST API
description: Send AI agent knowledge gaps and proposed corrections to Kebivo over REST. Learn project token authentication, idempotency, retries and response codes.
---

# REST integration guide

Send knowledge gaps and corrections with `POST /api/v1/feedback`. Your application decides when to report, so REST can collect every gap you choose to record. For reports initiated by an agent, see the [MCP guide](mcp.md).

The examples below assume Kebivo runs at `http://127.0.0.1:8000` (see the [quickstart](../README.md#quickstart)). Use your HTTPS URL in production.

## Authentication

Every request needs a **project token** in the `Authorization` header:

```http
Authorization: Bearer <PROJECT_TOKEN>
```

Create a token in the admin UI (open a project, then **Project tokens**) or with `POST /api/v1/projects/{project_id}/tokens`. The value is shown once; Kebivo stores only a keyed digest. A token belongs to one project, so you do not send a project ID.

| Scope | Allows |
| --- | --- |
| `feedback:gap` | `knowledge_gap` events. The default scope. |
| `feedback:correction` | `knowledge_correction` events, and only while the project's corrections policy is enabled. Grant it explicitly. |

Tokens can have an expiry date and can be revoked at any time. A token stops working when it expires or is revoked, or when its project is disabled or archived. Project tokens cannot call admin APIs, and an admin session cannot submit arbitrary feedback through `POST /api/v1/feedback`. Keep tokens in a secret store or environment variable, never in source code or client-side JavaScript.

An administrator can send a sample gap with `POST /api/v1/projects/{project_id}/test-event` using the session cookie, an allowed `Origin`, and `X-CSRF-Token`. Project bearer tokens receive 401. The request has no body; a successful response is `202` with `event_id` and `group_id`. It adds the `kebivo-test` tag and follows the same ingestion, grouping, routing and delivery path as a REST report. Each administrator can send 10 test events per minute; excess requests return 429 with `Retry-After`. Archived or disabled projects return 409.

## Request

```bash
curl -i http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <PROJECT_TOKEN>' \
  -H 'Idempotency-Key: 6f1c2b8e-0d55-4c3e-9a51-2f7f3c0e9b10' \
  --data '{"event_type":"knowledge_gap","question":"How do I reset my corporate VPN?","tags":["it","internal"],"agent_id":"employee-assistant"}'
```

The body must be a JSON object sent as `application/json`. Unknown fields are rejected. Text is trimmed and must not contain control characters. The whole body is limited to 131,072 bytes.

| Field | Event types | Rules |
| --- | --- | --- |
| `event_type` | Both | Required: `knowledge_gap` or `knowledge_correction`. |
| `question` | Both | Required; 1–10,000 characters. The question that could not be answered, or the one the correction is about. |
| `correction` | Correction | Required for corrections; 1–20,000 characters. Stored as an unverified proposal for human review. |
| `source_reference` | Correction | Up to 2,000 characters, for example a document link the user supplied. |
| `previous_answer` | Both | Up to 10,000 characters: what the assistant answered. |
| `context` | Both | Up to 4,000 characters of relevant context. |
| `agent_id` | Both | Up to 128 characters, identifies the reporting assistant. |
| `conversation_id`, `source_event_id`, `user_reference` | Both | Up to 256 characters each. Your own identifiers; Kebivo does not interpret them. |
| `tags` | Both | Up to 20 strings, each 1–64 characters after normalization. Normalized, deduplicated and combined with the project's default tags. Tags drive [routing](routing.md). |
| `metadata` | Both | JSON object, at most 8,192 bytes serialized and 8 levels deep. |

Do not send secrets or more personal data than the knowledge maintainers need: the event is stored unencrypted and may be delivered to webhooks and email.

## Response

`202 Accepted` means the event, its group membership and a processing job are stored durably. Routing and delivery happen afterwards in the background worker.

```json
{"event_id":"0b9a6f8e-...","status":"accepted","processing_status":"pending"}
```

A replayed request (see below) returns the same body with `202` and the header `Idempotent-Replayed: true`.

## Idempotency

Send an `Idempotency-Key` header (1–255 printable ASCII characters, no spaces) and **reuse the same key for every retry of the same report**. A random UUID per report works well.

- Same key, same content, within `IDEMPOTENCY_WINDOW_HOURS` (24 hours by default): Kebivo returns the original `event_id` and creates nothing new.
- Same key, different content: `409 idempotency_conflict`.
- No key: every request creates a new event. A retry after a timeout may then store the report twice. Exact deduplication groups the copies, but both events exist.

Keys are scoped to the project and shared with MCP: an MCP `idempotency_key` and a REST `Idempotency-Key` with the same value refer to the same record.

## Errors

Errors use one envelope:

```json
{"error":{"code":"validation_error","message":"Invalid request","details":{"fields":[{"field":"question","code":"string_too_short","message":"Invalid value"}]},"request_id":"..."}}
```

The `request_id` is also in the `X-Request-ID` response header; include it when you report a problem. Error messages never echo submitted content.

| Status | `code` | Meaning | Retry? |
| --- | --- | --- | --- |
| 400 | `bad_request`, `invalid_idempotency_key` | Malformed JSON, a body that is not an object, or an invalid `Idempotency-Key`. | No. Fix the request. |
| 401 | `unauthorized` | Token missing, invalid, expired or revoked, or the project is disabled or archived. | No. Fix the token. |
| 403 | `scope_required`, `corrections_disabled` | The token lacks the scope for this event type, or the project does not accept corrections. | No. |
| 409 | `idempotency_conflict` | The key was used for different content. | No. Use a new key for a new report. |
| 413 | `payload_too_large` | The body exceeds 131,072 bytes. | No. Shorten it. |
| 415 | `unsupported_media_type` | `Content-Type` is not `application/json`. | No. |
| 422 | `validation_error` | A field is missing, unknown, too long or has the wrong type. `details.fields` lists each problem. | No. |
| 429 | `rate_limited` | Too many requests. | Yes, after `Retry-After` seconds. |
| 5xx | `internal_error` or a proxy error | Server or proxy failure. | Yes, with backoff and the same key. |

Network errors and timeouts are also safe to retry with the same idempotency key.

## Retries and rate limits

Retry only `429`, `5xx` and network errors. Use exponential backoff (for example 1, 2, 4, 8 seconds), wait at least `Retry-After` on `429`, keep the same `Idempotency-Key`, and give up after a few attempts. Never retry other `4xx` answers unchanged.

Default limits are per one-minute window: 120 submissions per token (`FEEDBACK_RATE_LIMIT_PER_TOKEN`, shared with MCP tool calls) and 300 requests per client IP (`FEEDBACK_RATE_LIMIT_PER_IP`). Repeated authentication failures from one IP also count. Counters are kept in memory and reset when Kebivo restarts.

Reporting feedback should never break your user's conversation. Send it in the background, or catch errors and log them, so a failed report does not fail the answer.

## Python SDK

The [standalone Python SDK](../sdk/python/README.md) provides `Kebivo(url, token, timeout=10)`, `report_gap(...)`, and `submit_correction(...)`. It creates and reuses an idempotency key across retries, handles `Retry-After`, and raises typed errors. Install it directly from GitHub; it is not published to PyPI.

## Examples

Each example sends one knowledge gap, reuses one idempotency key across retries, retries `429` and `5xx` with backoff, and stops on other errors. They read `KEBIVO_URL` (default `http://127.0.0.1:8000`) and `KEBIVO_TOKEN`. The acceptance suite (`make e2e`) runs every file in [`examples/`](../examples/) against a built container, and the project page in the admin UI shows the same snippets with your project's endpoint.

### cURL

[`examples/rest_curl.sh`](../examples/rest_curl.sh) uses curl's built-in retry, which honours `Retry-After`:

```sh
export KEBIVO_TOKEN='<PROJECT_TOKEN>'
bash examples/rest_curl.sh "How do I reset my corporate VPN?"
```

### Python

[`examples/rest_python.py`](../examples/rest_python.py) needs only the standard library (Python 3.10 or later):

```sh
export KEBIVO_TOKEN='<PROJECT_TOKEN>'
python3 examples/rest_python.py --question "How do I reset my corporate VPN?" --tag it --tag vpn
```

The core of it:

```python
import json, urllib.request, uuid

request = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/feedback",
    data=json.dumps({"event_type": "knowledge_gap", "question": "How do I reset my corporate VPN?", "tags": ["it"]}).encode(),
    method="POST",
    headers={
        "Authorization": "Bearer <PROJECT_TOKEN>",
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4()),  # reuse this value on every retry
    },
)
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.status, json.load(response)["event_id"])
```

It exits with `0` when the report is accepted, `1` when it is rejected or retries run out, and `2` when `KEBIVO_TOKEN` is missing.

### TypeScript

[`examples/rest_typescript.ts`](../examples/rest_typescript.ts) uses the built-in `fetch`. Node.js 24 runs it directly, with no build step:

```sh
export KEBIVO_TOKEN='<PROJECT_TOKEN>'
node examples/rest_typescript.ts "How do I reset my corporate VPN?"
```

The core of it:

```ts
const response = await fetch("http://127.0.0.1:8000/api/v1/feedback", {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.KEBIVO_TOKEN}`,
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(), // reuse this value on every retry
  },
  body: JSON.stringify({ event_type: "knowledge_gap", question: "How do I reset my corporate VPN?", tags: ["it"] }),
});
if (response.status !== 202) throw new Error(`Kebivo answered ${response.status}`);
```

Run it on a server only. A browser would expose the project token.

### Corrections

Send a correction only for an explicit, user-supplied fix, with a token that has `feedback:correction`:

```json
{"event_type":"knowledge_correction","question":"What is the VPN support number?","correction":"The number changed to +1 555 0100 in March.","source_reference":"https://intranet.example.org/it/vpn","previous_answer":"Call +1 555 0199."}
```

Kebivo stores it as `unverified`. It never changes your knowledge base: a person reviews it.

## After submission

Track the event in the admin inbox (`/inbox`), where it appears in a feedback group. Delivery status for each destination is in the delivery log (`/deliveries`). Webhook receivers get the [versioned payload](destinations.md#webhook-payload-and-verification), and [`examples/webhook_receiver.py`](../examples/webhook_receiver.py) shows how to verify it.

Kebivo does not serve an OpenAPI document or interactive API docs. For agent prompts that decide when to report, start from [`examples/agent-instructions.md`](../examples/agent-instructions.md).
