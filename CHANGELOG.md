# Changelog

## 0.1.0 — 2026-10-25

Kebivo's first open-source release provides local administration, project-scoped REST and MCP feedback intake, exact and optional semantic grouping, inbox triage, routing to webhook, SMTP, and local inbox destinations, durable retry and restart recovery, retention, and project JSON Lines export. The default installation needs no external account or model.

Retention is enabled by default: feedback older than 180 days, terminal delivery attempt details older than 30 days, and audit records older than 90 days are removed. Set the corresponding environment variable to `0` to disable a class. Backups taken before a retention run still contain older data.

Documentation covers the quickstart, a REST integration guide with runnable cURL, Python and TypeScript examples, the MCP guide, a self-hosting guide (settings, TLS, backup and restore, upgrades, credential rotation, troubleshooting), contribution and security policies, and a third-party license inventory. Kebivo is licensed under Apache-2.0; contributions use a Developer Certificate of Origin sign-off and no CLA.

The container image is published as `ghcr.io/ocnai/kebivo:0.1.0` for `linux/amd64` and `linux/arm64`.

Fixed before release: SMTP delivery tries each allowed address of the mail host in resolver order, instead of failing when the first address refuses the connection.
