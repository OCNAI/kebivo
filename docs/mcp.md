---
title: Report AI knowledge gaps with MCP
description: Connect an MCP client to Kebivo to report unanswered questions and submit proposed corrections for human review, using scoped project tokens.
---

# MCP integration guide

Kebivo serves MCP Streamable HTTP at `{origin}/mcp`, where `origin` is the URL
of your running app, such as `https://feedback.example.org`. Configure a project
bearer token in the client as `Authorization: Bearer <PROJECT_TOKEN>`. Create the
token in the admin UI (select a project, then **Project tokens**) or with
`POST /api/v1/projects/{project_id}/tokens`; its value is shown once. The
project page's **Connect an application** panel (MCP tab) shows the endpoint and
ready-made client snippets. Admin cookies do not authenticate MCP. The endpoint uses stateless requests and JSON responses. It
does not support streaming server notifications or resumable sessions. `GET` and
`DELETE` on `/mcp` return 405. The tested protocol versions are `2026-07-28`
and `2025-11-25`.

MCP must be enabled at startup (`MCP_ENABLED=true`, the default). With
`MCP_ENABLED=false`, `/mcp` is absent. The MCP server does not offer OAuth
discovery or token exchange. Clients that require OAuth cannot connect; use a client that accepts a manually configured bearer token.

## Tool visibility

`tools/list` checks the token scopes and current project policy on every call.
The same checks run before each tool call. A policy or token-scope change is
visible at the next `tools/list` or reconnect; a previously listed tool can
stop working immediately if permission is removed. An inactive or archived
project, or an expired or revoked token, cannot connect.

| Token scopes | Project corrections policy | Visible tools |
| --- | --- | --- |
| `feedback:gap` | Either | `report_knowledge_gap` |
| `feedback:correction` | Enabled | `submit_knowledge_correction` |
| `feedback:correction` | Disabled | None |
| Both scopes | Enabled | Both tools |
| Both scopes | Disabled | `report_knowledge_gap` |
| Neither scope | Either | None |

For customer-facing bots, issue a token with only `feedback:gap` so they see
only the gap tool. Give correction access only to a trusted workflow
that handles explicit user-provided corrections. Tool availability does not
guarantee a model will call it. Use the REST `POST /api/v1/feedback` endpoint
when collection must happen reliably (see [Limitations](#limitations-of-model-initiated-tool-calls) and the [REST guide](rest-api.md)).

## Tool reference

Both tools accept a JSON object. Unknown fields are rejected. Text is trimmed;
required text must remain nonempty, and optional blank text becomes absent.
Text rejects control characters. Limits below are maximum character counts for
accepted values. The 20-tag cap applies to submitted items before deduplication.
The total HTTP request body limit is 131,072 bytes.

| Input | Gap tool | Correction tool | Bound / meaning |
| --- | --- | --- | --- |
| `question` | Required | Required | 1–10,000 characters; the question or unmet information need. |
| `correction` | — | Required | 1–20,000 characters; proposed answer or correction for review. |
| `context` | Optional | — | Up to 4,000 characters of relevant context. |
| `previous_answer` | Optional | Optional | Up to 10,000 characters; the prior answer, if any. |
| `source_reference` | — | Optional | Up to 2,000 characters; source supplied with the correction. |
| `tags` | Optional | Optional | Up to 20 strings; each display name and normalized tag must be 1–64 characters. Tags are normalized and deduplicated. |
| `source_event_id` | Optional | Optional | Up to 256 characters; caller's source event identifier. |
| `conversation_id` | Optional | Optional | Up to 256 characters; caller's conversation identifier. |
| `agent_id` | Optional | Optional | Up to 128 characters; caller's agent identifier. |
| `idempotency_key` | Optional | Optional | 1–255 printable ASCII characters, without spaces; use only when retrying the identical submission. |

`report_knowledge_gap` records an unanswered or insufficiently supported
question for follow-up. `submit_knowledge_correction` records a correction or
proposed answer **for human review**; it is unverified and must not be treated as
authoritative knowledge. Each successful call stores a feedback event and a
pending processing job, exactly like a REST submission: the worker then groups,
routes and delivers it to the project's destinations. Events appear in the admin
inbox (`/inbox`) with channel `mcp`.

The tool result has `structuredContent` and an equivalent JSON text block in
`content`. The gap result is:

```json
{"event_id":"<EVENT_ID>","status":"accepted"}
```

The correction result also includes `"verification_status":"unverified"`.
A successful replay includes `"replayed":true`. The `replayed` property is
omitted on a new submission. MCP's `accepted` status means durable acceptance;
the stored processing job remains pending. The response does not expose the
submitted content.

Idempotency is shared with REST. The MCP `idempotency_key` uses the same
project-scoped records and configured window as REST's `Idempotency-Key`
header (`IDEMPOTENCY_WINDOW_HOURS`, default 24, range 1–720). A retry of
the identical normalized submission returns the original `event_id`, even
across MCP and REST. Reusing the key for different content returns a conflict.

## Errors

| Response | Meaning |
| --- | --- |
| HTTP 401 `unauthorized` | Bearer token missing, invalid, expired, or revoked; project disabled or archived. Includes `WWW-Authenticate: Bearer`. |
| HTTP 413 `payload_too_large` | Request body exceeds 131,072 bytes. |
| HTTP 421 | `Host` is outside the MCP Host allowlist. |
| HTTP 403 | Supplied `Origin` is outside `ALLOWED_ORIGINS`. |
| HTTP 429 `rate_limited` | Per-IP request limit (`FEEDBACK_RATE_LIMIT_PER_IP`) reached; see `Retry-After`. |
| JSON-RPC `Unknown tool: <name>` (`-32602`) | Tool name is unknown or no longer authorized. Hidden tools are not distinguished. |
| Tool result `validation_error` | Invalid or extra input; `isError: true`, with `error.fields[]` containing `field`, `code`, and `message`. |
| Tool result `idempotency_conflict` | Key was already used for a different submission; `isError: true`. |
| Tool result `rate_limited` | Per-token limit (`FEEDBACK_RATE_LIMIT_PER_TOKEN`, shared with REST) reached; `isError: true` and `retry_after_seconds`. |
| Tool result `internal_error` | Unexpected failure; `isError: true` and a `request_id`. |

HTTP errors use `{ "error": { "code", "message", "request_id" } }` where the
application handles them; MCP transport security and JSON-RPC errors may use
their protocol-native shapes. A validation tool result, for example, has
`structuredContent` shaped as
`{"error":{"code":"validation_error","fields":[{"field":"question","code":"...","message":"Invalid value"}]}}`.
The server logs request metadata and error types, never token values, tool
arguments, question or correction content, or response bodies. Treat the local
database and any upstream proxy logs according to your own privacy policy.

## Client configuration

Replace only the placeholders. Keep project tokens in private client settings.
The public URL must use HTTPS except for local loopback development.

Claude Code:

```sh
claude mcp add --transport http <name> <url> --header "Authorization: Bearer <PROJECT_TOKEN>"
```

Use the full MCP endpoint as `<url>`, for example
`https://feedback.example.org/mcp`.
The `examples/mcp_client.py --url` option takes the origin, while `claude mcp add` takes the full `/mcp` URL.

VS Code-style `mcp.json`:

```json
{
  "servers": {
    "feedback": {
      "type": "http",
      "url": "https://feedback.example.org/mcp",
      "headers": {
        "Authorization": "Bearer <PROJECT_TOKEN>"
      }
    }
  }
}
```

[`examples/mcp-client-config.json`](../examples/mcp-client-config.json) is the
`mcpServers` form used by Claude Code's project `.mcp.json` and similar clients.
It reads the token from the `KEBIVO_TOKEN` environment variable
(`${KEBIVO_TOKEN}`), so the file itself holds no secret.

The project page's **Connect an application** panel shows these snippets with
your project's endpoint filled in. It does not insert a real token.

The [Python SDK example](../examples/mcp_client.py) lists visible tools and
optionally submits one knowledge gap. From this repository after `make install`:

```sh
PROJECT_TOKEN='<PROJECT_TOKEN>' uv run python examples/mcp_client.py --url http://127.0.0.1:8000
PROJECT_TOKEN='<PROJECT_TOKEN>' uv run python examples/mcp_client.py --url http://127.0.0.1:8000 --question 'Where is the VPN setup guide?'
```

The acceptance suite (`make e2e`) runs this example and
[`examples/mcp-client-config.json`](../examples/mcp-client-config.json) against a
built container.

## Test the connection

1. Create a project token with `feedback:gap` and copy it.
2. List the tools: run the first `mcp_client.py` command above. You should see
   `report_knowledge_gap`; `submit_knowledge_correction` appears only for a
   token with `feedback:correction` on a project that accepts corrections.
3. Submit a test gap with the `--question` command and note the `event_id`.
4. Open `/inbox` in the admin UI: the event is there with channel `mcp`. Its
   deliveries are in `/deliveries`.

In Claude Code, run `claude mcp list` to check the connection, then ask the
agent to report a test knowledge gap. Any other MCP client that supports
Streamable HTTP with a custom header works the same way. A `401` means the token
is wrong, expired or revoked, or the project is disabled; a `421` means the
`Host` is not allowed (see below).

## Limitations of model-initiated tool calls

With MCP the **model decides** whether and when to call a tool. It may not call
it when it should, may call it for a question it actually answered, may
paraphrase the question, or may put information in the wrong field. Tool
descriptions and [agent instructions](../examples/agent-instructions.md)
can improve reporting, but cannot guarantee it.

- Use REST from your application code when every gap must be recorded.
- Review what arrives in the inbox and adjust your agent's instructions.
- Give customer-facing agents only `feedback:gap`. Corrections from MCP are
  always stored as unverified proposals for human review.
- Content a model sends is untrusted user text. Kebivo stores and displays it as
  plain text and never executes it.

## Host and reverse proxy setup

The per-IP limit counts every `/mcp` HTTP request before authentication. One
tool call typically makes 2–3 requests, and the same bucket is shared with REST
feedback requests. Set `TRUSTED_PROXY_IPS` to the proxy's peer IP behind a proxy
so the limit uses forwarded client IPs.

`ALLOWED_ORIGINS` is a comma-separated list of exact browser origins, such as
`https://feedback.example.org`. The MCP Host allowlist includes loopback hosts,
hosts from `ALLOWED_ORIGINS`, and any extra comma-separated host or `host:port`
entries in `MCP_ALLOWED_HOSTS`. `MCP_ALLOWED_HOSTS` values have no scheme or
path. Configure the external host seen by MCP clients. A proxy should forward
the original `Host` and terminate TLS, for example with
`proxy_set_header Host $host` in Nginx. If the public URL uses a nondefault
port, include that port in the allowlist entry. `Origin`, when a client sends
it, must match an entry in `ALLOWED_ORIGINS`; browser-origin policy and Host
policy are separate checks. Because hosts from `ALLOWED_ORIGINS` are already
allowed, a proxy serving the same public host as the admin UI needs no
`MCP_ALLOWED_HOSTS` entry. Restart the app after changing these settings.

Keep the app bound to loopback behind the proxy. Preserve the Authorization
header when proxying `/mcp`. See the
[reverse proxy section of the self-hosting guide](self-hosting.md#reverse-proxy-and-tls)
for an Nginx starting point.

## Registry listing

`server.json` holds the draft MCP Registry entry. See
[mcp-registry.md](mcp-registry.md).
