# Changelog

## Unreleased

- Optional embeddings grouping compares question vectors from recent group representatives using an Ollama or OpenAI-compatible endpoint. It includes backfill, an audited preview with question excerpts, a `kind=embeddings` provider test and a `dedup-eval` threshold CLI; dimensions are inferred, input is lowercased, and the default similarity threshold is 0.93. Exact grouping remains the default, and failed embedding calls leave intake and routing available.

## 0.2.0 — 2026-10-27

A new installation now gets from `docker run` to a routed, signed delivery by copy-paste, with no UI step and no model key.

- `python -m kebivo.cli project create --name "..."` creates a project and prints a `feedback:gap` token once (`--json` for scripts). `bootstrap --password-stdin` reads the admin password from a pipe without warnings.
- The quickstart leads with the published image, uses the real clone URL for the build path, and adds a "Route it" step: run `examples/webhook_receiver.py`, add a webhook destination and rule, and see the receiver verify a signed delivery. A test keeps the documented image tag in step with the package version.
- The project Connect panel has a "Send test event" button that sends a sample gap through normal intake and routing and links to its group. Inbox and dashboard empty states point to it.
- Signing in from an origin that is not allowed now returns a message that lists the configured origins instead of a bare "Invalid Origin".
- Native Slack destination: paste a Slack incoming-webhook URL and Kebivo posts a Block Kit message with the question, project, tags, occurrence count and, when `PUBLIC_URL` is set, a link to the group. The URL is validated to `hooks.slack.com`, stored encrypted and never shown again in full.
- Python client (`sdk/python`, standard library only) and TypeScript client (`sdk/typescript`, ESM, no runtime dependencies, Node 20+) for reporting gaps and corrections, with idempotency keys, bounded retries that honour `Retry-After`, and typed errors. An optional LangChain callback reports a gap when a retriever returns no documents. Install from Git; neither is published to a package index.
- Group detail shows when each occurrence was received.

## 0.1.1 — 2026-10-25

Packaging only; no application changes. The MCP Registry server name is now `io.github.OCNAI/kebivo`, in `server.json` and in the image's `io.modelcontextprotocol.server.name` label. The registry matches the GitHub account's letter case, so it refused the lower-case name used in 0.1.0.

## 0.1.0 — 2026-10-25

Kebivo's first open-source release provides local administration, project-scoped REST and MCP feedback intake, exact and optional semantic grouping, inbox triage, routing to webhook, SMTP, and local inbox destinations, durable retry and restart recovery, retention, and project JSON Lines export. The default installation needs no external account or model.

Retention is enabled by default: feedback older than 180 days, terminal delivery attempt details older than 30 days, and audit records older than 90 days are removed. Set the corresponding environment variable to `0` to disable a class. Backups taken before a retention run still contain older data.

Documentation covers the quickstart, a REST integration guide with runnable cURL, Python and TypeScript examples, the MCP guide, a self-hosting guide (settings, TLS, backup and restore, upgrades, credential rotation, troubleshooting), contribution and security policies, and a third-party license inventory. Kebivo is licensed under Apache-2.0; contributions use a Developer Certificate of Origin sign-off and no CLA.

The container image is published as `ghcr.io/ocnai/kebivo:0.1.0` for `linux/amd64` and `linux/arm64`.

Fixed before release: SMTP delivery tries each allowed address of the mail host in resolver order, instead of failing when the first address refuses the connection.
