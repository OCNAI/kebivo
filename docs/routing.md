---
title: Route AI agent feedback to email and webhooks
description: Configure Kebivo's ordered tag rules, fallback destinations and simulator to route AI knowledge gaps and corrections for review.
---

# Routing rules and simulator

Routing configuration is per project. Each accepted feedback event remains in the built-in inbox. The embedded worker evaluates rules and creates deliveries after ingestion commits. `WORKER_ENABLED=false` pauses processing and sending without rejecting submissions.

Create rules with `POST /api/v1/projects/{id}/routing-rules`, list them with `GET`, and update or delete with `PATCH` or `DELETE /api/v1/projects/{id}/routing-rules/{rule_id}`. Admin cookie authentication is required, and writes require `Origin` and `X-CSRF-Token`. A project supports at most 100 rules.

A rule has a name, enabled flag, event types (`knowledge_gap` and/or `knowledge_correction`), tags, match mode, priority, destination IDs and match behavior. Tags use project tag normalization and are created in the project if missing. An empty tag list matches every tag set. `any` matches when at least one rule tag is present; `all` needs every rule tag. Event type must also match. Rules run by ascending priority, then creation time, then ID. A matching `continue` rule allows subsequent rules to run; a matching `stop` rule prevents them. Repeated destinations result in one planned delivery per event.

Set `fallback_destination_id` with `PATCH /api/v1/projects/{id}`; `null` clears it. The fallback applies only when no enabled rule matches. It does not apply when a matched rule has disabled destinations or a delivery later fails. A disabled fallback destination is reported as skipped.

`POST /api/v1/projects/{id}/routing/simulate` accepts `{"event_type":"knowledge_gap","tags":["bug"],"include_default_tags":true}`. An optional `group_id` simulates another occurrence in that project and type; without it, the sample is a new group. It loads the current configuration and returns evaluated rules, match outcomes, fallback status and warnings. It does not create events, deliveries or routing matches and makes no network connection. Default project tags are included unless `include_default_tags` is false. The response has `evaluated_rules` entries with `rule_id`, `status`, and `reason`; `destinations` entries have `destination_id`, `via`, `outcome`, and `rule_id`. It also includes `stopped_by_rule_id`, `fallback_applied`, and `warnings`. Names and rule order are available from the rule list.

## Using the dashboard

Open a project in the Admin UI and use its Routing section to add rules in priority order. Set event types, choose ANY or ALL tag matching, select one or more enabled destinations, then choose whether a match continues evaluation or stops it. Rule tags are normalized before saving; project tags are shown as suggestions. The fallback selector applies only when no enabled rule matches.

In the simulator, choose an event type and tags. Keep **Include project default tags** enabled to model normal ingestion, or turn it off to isolate submitted tags. Select **Simulate** to see rule results, stop markers, planned destinations, fallback status and warnings. The simulator sends nothing.

Use **Delivery log** in the navigation to filter delivery records. Open a row to inspect each attempt and its sanitized result. A failed delivery can be manually retried only while its destination is enabled.

## Worker and delivery

One worker runs inside the API process. It claims due processing jobs before due deliveries, ordered by next attempt time then creation time, polls every `WORKER_POLL_INTERVAL_SECONDS` (default 1), and wakes after REST ingestion. `WORKER_CONCURRENCY` (default 4) limits simultaneous adapter calls. The worker uses short SQLite transactions; no transaction stays open while contacting a destination. The loop retries after transient database errors; `/health/ready` reports `worker: stopped` only if its task is cancelled or crashes unrecoverably.

Processing jobs move from `pending` to `processing` to `completed`, or return to `pending` after errors. They retry after 30 seconds, 2 minutes, 10 minutes and 30 minutes, then become `failed` with `routing_error`. Each successful evaluation replaces routing matches and creates at most one delivery per event and destination, even if the job runs again. A disabled destination is skipped at routing time. `new_groups_only` suppresses an already delivered group and records `suppressed_duplicate`; `every_occurrence` sends each routed event. Reopen and a destination with no earlier non-failed delivery are eligible. See [deduplication](deduplication.md). Existing pending deliveries to a destination are held while it is disabled.

Deliveries move from `pending` to `processing`, then `delivered`, `retry_scheduled`, or `failed`. An attempt is recorded before each send. Retryable failures use `DELIVERY_RETRY_SCHEDULE_SECONDS` (default `30,120,600,1800,7200`) with ±20% jitter. At most six automatic attempts run. A `Retry-After` response can extend the delay up to `DELIVERY_RETRY_AFTER_MAX_SECONDS` (default 7200); it cannot shorten the scheduled delay. Invalid stored configuration and unavailable secrets fail permanently. Deliveries are at least once: a timeout followed by a retry can post a duplicate Slack message or webhook. Webhook receivers should deduplicate on `delivery_id` or the `X-Delivery-ID` header.

Deleting a feedback event cancels its pending and scheduled deliveries and removes its delivery history. Deletion returns 409 while any delivery or processing job is in progress. Copies already sent to a destination cannot be recalled.

Each job lease lasts 60 seconds. A delivery lease lasts its configured timeout plus 30 seconds. At startup, a new worker recovers jobs and deliveries left in `processing` by another boot: jobs return to `pending`; open delivery attempts become `interrupted`, with remaining-budget deliveries due for retry. On shutdown, claiming stops and in-flight calls have up to `WORKER_SHUTDOWN_GRACE_SECONDS` (default 20) to record their results. Any cancelled call keeps its lease for recovery after restart.

Administrators can inspect `GET /api/v1/deliveries` with `status`, `destination_id`, `project_id`, `event_id`, `limit`, and `cursor`; `GET /api/v1/deliveries/{id}` includes attempt logs. `POST /api/v1/deliveries/{id}/retry` performs one audited manual attempt for a failed delivery with an enabled destination. `POST /api/v1/destinations/{id}/test` sends a synthetic `test:true` payload, including to disabled destinations, and writes only an audit event. It is limited to ten calls per minute per session. Admin feedback detail includes routing matches and delivery summaries.

Webhook redirects return the permanent `redirect_not_followed` category. Webhook TLS connection failures are retryable; SMTP certificate failures are permanent. Both use the sanitized `tls` category.
