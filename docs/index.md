---
title: Documentation
description: Technical documentation for Kebivo, a self-hosted feedback router for AI assistant knowledge gaps. Install, connect over REST or MCP, route and operate.
---

# Kebivo documentation

Kebivo collects knowledge gaps from AI assistants in a self-hosted inbox. Application code can send reports over REST; agents can use an MCP tool. Kebivo groups repeated questions and routes them to the team that maintains the docs. It does not answer questions or edit your knowledge base.

Use these pages for setup, integration and operations. Tutorials and framework guides are in the [Kebivo website Guides](website:guides/). The [repository](../README.md) has the source code and runnable examples.

## Get started

- [Quickstart](quickstart.md): build and run Kebivo with Docker Compose, create a project token and send a first report.
- [Self-hosting](self-hosting.md): production deployment, configuration, backups and upgrades.

## Connect an assistant

- [REST API](rest-api.md): submit gaps and corrections from application code, with idempotency and retries.
- [MCP](mcp.md): give an MCP client the `report_knowledge_gap` tool, and understand scopes and tool visibility.

## Review and route feedback

- [Feedback management](feedback-management.md): the inbox, statuses, notes, merge and split.
- [Deduplication](deduplication.md): exact grouping by default and optional semantic grouping.
- [Routing](routing.md): ordered tag rules, fallback and the routing simulator.
- [Destinations](destinations.md): email and signed webhooks, and delivery history.
- [LLM providers](llm-providers.md): configure the optional semantic grouping provider and its data flow.

## Data and releases

- [Retention](retention.md) and [Export](export.md): data retention rules and JSON Lines export.
- [Releasing](releasing.md) and [Licenses](licenses.md): the release process and third-party licenses.

A REST `202 Accepted` means a report is stored for processing, not that a destination has received it. Webhook and email delivery is at least once.
