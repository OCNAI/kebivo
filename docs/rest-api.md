---
title: Collect AI knowledge gaps with the REST API
description: Send AI agent knowledge gaps and proposed corrections to Kebivo over REST. Learn project token authentication, idempotency, retries and response codes.
---

# REST integration guide

Send knowledge gaps and corrections with `POST /api/v1/feedback`. Your application decides when to report, so REST can collect every gap you choose to record. For reports initiated by an agent, see the [MCP guide](mcp.md).

For a small fallback you can adapt to your chatbot, see [Report from your own app](report-from-your-app.md).

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
| `gaps:read` | Reading the project's knowledge gaps with [`GET /api/v1/groups`](#read-open-gaps). Off unless you grant it. |

Tokens can have an expiry date and can be revoked at any time. A token stops working when it expires or is revoked, or when its project is disabled or archived. Project tokens cannot call admin APIs, and an admin session cannot submit arbitrary feedback through `POST /api/v1/feedback`. Keep tokens in a secret store or environment variable, never in source code or client-side JavaScript.

An administrator can send a sample gap with `POST /api/v1/projects/{project_id}/test-event` using the session cookie, an allowed `Origin`, and `X-CSRF-Token`. Project bearer tokens receive 401. The request has no body; a successful response is `202` with `event_id` and `group_id`. Its question is `Kebivo test event: How do I reset my corporate VPN?`; it adds the `kebivo-test` tag and follows the same ingestion, grouping, routing and delivery path as a REST report. Each administrator can send 10 test events per minute; excess requests return 429 with `Retry-After`. Storage contention returns `503 storage_busy` with `Retry-After: 1`. Test events have no idempotency key, so retrying after an uncertain network result can create another event. Archived or disabled projects return 409.

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
| 503 | `storage_busy` | SQLite could not acquire a writer lock or pooled connection within the bounded wait. No acceptance is reported. | Yes, after `Retry-After` seconds with the same key. |
| 5xx | `internal_error` or a proxy error | Server or proxy failure. | Yes, with backoff and the same key. |

Network errors and timeouts are also safe to retry with the same idempotency key.

## Retries and rate limits

Retry only `429`, `5xx` and network errors. Use exponential backoff (for example 1, 2, 4, 8 seconds), wait at least `Retry-After` on `429`, keep the same `Idempotency-Key`, and give up after a few attempts. Never retry other `4xx` answers unchanged.

Default limits are per one-minute window: 120 submissions per token (`FEEDBACK_RATE_LIMIT_PER_TOKEN`, shared with MCP tool calls) and 300 requests per client IP (`FEEDBACK_RATE_LIMIT_PER_IP`). Repeated authentication failures from one IP also count. Counters are kept in memory and reset when Kebivo restarts.

Reporting feedback should never break your user's conversation. Send it in the background, or catch errors and log them, so a failed report does not fail the answer.

## Read open gaps

A token with the `gaps:read` scope can list its project's knowledge gaps, for example from a nightly job that writes the top questions into your docs repo. Correction groups are not included.

```bash
curl -s "$KEBIVO_URL/api/v1/groups?status=open&sort=occurrences&limit=2" \
  -H "Authorization: Bearer $KEBIVO_READ_TOKEN"
```

```json
{
  "items": [
    {
      "id": "70a3b6b0-e8a1-49e3-9995-d6b27364d162",
      "question": "How do I paginate the /orders endpoint?",
      "occurrence_count": 3,
      "first_seen_at": "2026-11-16T08:16:35.420900+00:00",
      "last_seen_at": "2026-11-16T08:16:35.666357+00:00",
      "tags": ["api"],
      "status": "open"
    },
    {
      "id": "c9d5e51b-7407-4070-bacc-d0d60c968479",
      "question": "Can I rotate an API key without downtime?",
      "occurrence_count": 2,
      "first_seen_at": "2026-11-16T08:16:35.741371+00:00",
      "last_seen_at": "2026-11-16T08:16:35.810099+00:00",
      "tags": ["api"],
      "status": "open"
    }
  ],
  "next_cursor": "eyJ2IjpbMiwi…"
}
```

| Parameter | Values | Default |
| --- | --- | --- |
| `status` | `open`, `in_progress`, `resolved`, `dismissed`; repeat to combine | `open` |
| `sort` | `occurrences` (most asked first, then most recent) or `last_seen` (most recent first) | `occurrences` |
| `limit` | 1–50 | 20 |
| `cursor` | `next_cursor` from the previous page, with the same `status` and `sort` | none |

Each item has only the fields shown: the group's representative question (up to 1,000 characters), how many times it was asked, when it was first and last seen, its tag slugs (up to 20) and its status. Admin notes, `user_reference`, context, previous answers, other reports in the group and delivery details are never returned. A token without `gaps:read` receives `403`; an invalid value returns `422` and an invalid cursor `400`. Reads are limited to 30 per minute per token (`GAPS_READ_RATE_LIMIT_PER_TOKEN`, shared with the MCP `list_open_gaps` tool) and also count toward the per-IP limit. Each successful read writes an audit event that records the token, the filters and the number of items, never the question text.

The questions are text your end users typed, and can include names, email addresses or other personal details. Treat a `gaps:read` token like a database read credential:

- Create a separate token for reading. Never grant `gaps:read` to the token your assistant or app uses to report gaps. The API accepts a token with only `gaps:read`; the admin UI adds it to a `feedback:gap` token.
- Keep it in a secret store and out of public CI logs.
- Revoke it as soon as it is no longer needed. Revocation takes effect on the next request.

## Python SDK

The [standalone Python SDK](../sdk/python/README.md) provides `Kebivo(url, token, timeout=10)`, `report_gap(...)`, and `submit_correction(...)`. It creates and reuses an idempotency key across retries, handles `Retry-After`, and raises typed errors. Install it directly from GitHub; it is not published to PyPI.

### LangChain and LangGraph

Install the `kebivo-client[langchain]` extra with its optional callback dependency from a repository checkout:

```sh
pip install './sdk/python[langchain]'
```

Attach the handler to a LangChain retriever. An empty document list reports the retriever query as a knowledge gap. A nonempty result does not report. Reports use a bounded background queue and one worker, so Kebivo retries never delay retrieval. A full queue drops reports with a warning, and delivery errors are logged rather than raised. Call `handler.flush(timeout=1)` before shutdown to wait for accepted reports.

```python
import os
from kebivo_client import Kebivo
from kebivo_client.langchain import KebivoCallbackHandler

client = Kebivo(os.environ["KEBIVO_URL"], os.environ["KEBIVO_TOKEN"])
handler = KebivoCallbackHandler(client, project_tags=["support"])
documents = retriever.invoke(question, config={"callbacks": [handler]})
```

In a LangGraph retrieval node, pass the graph's callback config through to the retriever so the handler sees its run:

```python
from langchain_core.runnables import RunnableConfig

def retrieve(state: dict, config: RunnableConfig) -> dict:
    docs = retriever.invoke(state["question"], config=config)
    return {"documents": docs}

result = graph.invoke(
    {"question": "Where is the VPN guide?"},
    config={"callbacks": [handler]},
)
```

Here `retriever`, `question`, and `graph` are your existing application objects; register `retrieve` as a node in the graph. This reports only an empty retrieval, without needing a model to decide to call a tool.

## TypeScript SDK

The [standalone TypeScript SDK](../sdk/typescript/README.md) provides the same calls and retry behavior for Node 20+ using global `fetch`, with no runtime dependencies. Install it by local path or copy its ESM file. It is not published to npm. Keep project tokens on the server; never embed them in browser code.

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

### Admin embeddings checks

Embedding dimensions are inferred from each provider response; there is no project dimension setting. Kebivo accepts consistent vectors of 1–8,192 finite values. Existing `embedding_dimensions` values in saved project settings are dropped when the settings are updated.

`POST /api/v1/admin/projects/{project_id}/dedup/preview` returns `question_excerpt` (up to 280 characters from the representative question) on each embeddings `similarities` item. Candidate lookup uses the query vector's dimensions, so vectors from an earlier model size are ignored.

`POST /api/v1/admin/llm-providers/{provider_id}/test` runs the existing chat test by default (`kind=chat`) with no request body. To check embeddings, call `POST /api/v1/admin/llm-providers/{provider_id}/test?kind=embeddings&embedding_model=nomic-embed-text`. `embedding_model` is optional; it falls back to a project setting for this provider, then to the provider model. The embeddings response has top-level `ok`, `error_category`, `dims`, `latency_ms`, `related_similarity`, `unrelated_similarity`, and `http_status`; for example, `{"ok":true,"error_category":null,"dims":768,"latency_ms":12,"related_similarity":0.87,"unrelated_similarity":0.21,"http_status":200}`. `ok` requires the related pair to score above the unrelated pair. Azure OpenAI returns `error_category: "unsupported"`. The test sends only fixed questions, never stored feedback. Input is lowercased before embedding, and the default similarity threshold for grouping is `0.93`.
