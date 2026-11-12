---
title: Documentation
description: "Your chatbot app reports each question it couldn't answer over REST or MCP. Kebivo groups repeats and routes them to whoever fixes the docs. Self-hosted, no signup."
---

# Kebivo documentation

Kebivo is a self-hosted inbox for questions your chatbot couldn't answer. Your app reports each one over REST or MCP; Kebivo groups repeats and routes them to whoever can fix the docs. No signup needed.

Kebivo does not answer questions or edit your knowledge base.

Use these pages for setup, integration and operations. Tutorials and framework guides are in the [Kebivo website Guides](website:guides/). The [repository](../README.md) has the source code and runnable examples.

## Get started

- [Quickstart](quickstart.md): run the published Kebivo image with Docker, create a project token and send your first report to the inbox.
- [Self-hosting](self-hosting.md): production deployment, configuration, backups and upgrades.

## Connect an assistant

- [REST API](rest-api.md): submit gaps and corrections from application code, with idempotency and retries.
- [MCP](mcp.md): give an MCP client the `report_knowledge_gap` tool, and understand scopes and tool visibility.

## Review and route feedback

- [Feedback management](feedback-management.md): the inbox, statuses, notes, merge and split.
- [Deduplication](deduplication.md): exact grouping by default, with an optional LLM judge or embeddings.
- [Routing](routing.md): ordered tag rules, fallback and the routing simulator.
- [Destinations](destinations.md): Slack, email and signed webhooks, and delivery history.
- [LLM providers](llm-providers.md): configure the optional semantic grouping provider and its data flow.

## Data and releases

- [Retention](retention.md) and [Export](export.md): data retention rules and JSON Lines export.
- [Releasing](releasing.md) and [Licenses](licenses.md): the release process and third-party licenses.

A REST `202 Accepted` means a report is stored for processing, not that a destination has received it. Webhook and email delivery is at least once.
