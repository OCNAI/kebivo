---
title: Documentation
description: Technical documentation for Kebivo, a self-hosted feedback router for AI assistant knowledge gaps. Install, connect over REST or MCP, route and operate.
---

# Kebivo documentation

Kebivo is an open-source, self-hosted tool that collects the questions your AI assistant, chatbot or RAG app couldn't answer, groups repeats, and routes them to the people who own the content. Your agent or app reports a gap over REST or MCP, and routing rules deliver it to an admin inbox, Slack, email or signed webhooks. It runs as one container with no cloud account or AI provider required, under Apache-2.0.

Kebivo does not answer questions or edit your knowledge base.

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
