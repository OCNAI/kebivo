---
title: Group repeated AI knowledge gaps
description: Learn how Kebivo groups repeated AI agent questions with exact matching and optional semantic deduplication, including provider and privacy tradeoffs.
---

# Deduplication

New projects use `deduplication: {"mode":"exact"}`. Migration preserves existing project behavior: `enabled: true` becomes `exact`, and `enabled: false` becomes `disabled`. Administrators can select `disabled`, `exact`, or `semantic`. Legacy `{ "enabled": true|false }` input is accepted on PATCH and mapped to a mode. Semantic mode requires an enabled provider and an explicit `privacy_acknowledged: true` in the settings request. The acknowledgement is audited.

In `disabled` mode, every submission starts a new group. In `exact` mode, REST and MCP ingestion find the newest earlier event with the same project, event type and version 2 question hash inside the submission transaction. The new event joins that earlier event's **current** group, so manual merge and split decisions remain meaningful. The original event content stays intact.

Version 2 normalization applies Unicode NFKC and case folding, maps typographic single and double quotes to plain quotes, maps U+2010 through U+2015 and U+2212 dashes to `-`, collapses whitespace, and removes one trailing run of `?`, `!`, `.`, `。`, `？`, or `！` with preceding spaces. It does not stem words, remove stop words, or remove internal punctuation. “VPN” and “VPS” remain different.

An exact occurrence increments the group's count and writes `occurrence_added` history. If the group was `resolved`, it reopens with a system status change and the occurrence has `triggered_reopen: true`. A `dismissed` group stays dismissed. Administrators can correct a grouping with group merge and split operations. Migration rehashes existing events, but changing a mode does not regroup stored events.

Destination mode `every_occurrence` delivers each routed event. `new_groups_only` delivers when the event created a group, reopened a resolved group, or the destination has no non-failed delivery for another member of that group. Otherwise routing records `suppressed_duplicate`. A previous failed delivery or deleted earlier event no longer blocks that destination. Webhook and email deliveries show the event's current group ID and occurrence count when sent.

`semantic` first does the same exact check during ingestion. An exact hit joins immediately without a model call. Other events receive a provisional group and a durable processing job. Choose `semantic.method: judge` (the default for semantic mode) or `semantic.method: embeddings`. The worker calls the configured provider outside a database transaction, then rechecks the lease and that the provisional group is untouched before it commits a match, deletes the provisional group, and routes the event. Any failure keeps the event in its own group and routing continues.

### Judge method

The judge method searches FTS5 for question-only candidates from the same project and event type. It sends at most 10 candidates labelled `c1`…`cN`; the provider never receives group IDs.

The provider receives only the new question and each candidate representative question, truncated to 2,000 characters in a JSON data block. It does **not** receive corrections, previous answers, context, tags, metadata, source or user references, agent or conversation IDs. Model text is untrusted; a response must match the exact answer schema and refer to an offered label. The default minimum confidence is 0.85. `LLM_MAX_CONCURRENCY` bounds simultaneous model calls. Retries apply only to timeout, connection, HTTP 429 and HTTP 5xx failures, under a 45 second total budget.

The worker claims at most `LLM_MAX_CONCURRENCY` semantic jobs at once and keeps at least one slot available for exact jobs or deliveries. Due deliveries are claimed first, followed by nonsemantic jobs. A semantic backlog therefore does not wait inside worker slots for the LLM semaphore or delay unrelated routing. With `WORKER_CONCURRENCY=1`, the worker uses a second slot for the semantic lane.

Semantic matching can be wrong. A feedback author can put instructions in question text that steer the model toward a false `same_issue` verdict. Matching stays within the same project and event type, and the original event is kept. A false match can reopen a resolved group or suppress a `new_groups_only` delivery. Administrators can raise `min_confidence` and use group merge or split to correct grouping.

Each judge-processed event has an evaluation outcome: `matched`, `no_match`, `low_confidence`, `invalid_response`, `invalid_candidate`, `no_candidates`, `provider_unavailable`, `provider_error`, `timeout`, or `skipped_group_changed`. The evaluation keeps the model's reason (unverified text) with the event; deleting the event deletes the reason. Logs include only IDs, outcome, category, attempts and latency. A disabled or exact project, an exact hit, or no candidates makes no model call.

Administrators can call `POST /api/v1/admin/projects/{id}/dedup/preview` with an event type and question to inspect an exact hit, FTS candidates and a model verdict without storing feedback. The call is audited without question content and is limited to 10 per minute per admin. `GET /api/v1/admin/groups/{id}/similar` lists up to 10 same-project, same-type candidate groups without a model call. Use group merge or split to correct a mistaken grouping; the original event text is preserved. Changing the mode does not revisit older events.

### Embeddings method

Embeddings compare the new question with stored vectors for the most recent `max_scan` group representatives in the same project and event type. There is no FTS candidate stage. Only vectors from the configured model and dimensions are compared. The highest cosine similarity joins its group when it meets `similarity_threshold`; otherwise the event keeps its own group. The default scan limit is 2,000 (range 1–5,000), and the threshold accepts 0.50–0.99. An exact hit still joins during intake without an embedding request. [Configure an embedding provider](llm-providers.md#ollama-recipe) before enabling this method.

The current default threshold is **0.88**. The local `nomic-embed-text` calibration on 150 balanced synthetic pairs exposed a tokenizer issue with capitalized input; lowercasing the input and raising the default to **0.93** is being implemented. With lowercased input, the short calibration table is:

| Threshold | Precision | Recall | True positives | False positives | False negatives |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0.85 | 0.320 | 0.107 | 8 | 17 | 67 |
| 0.88 | 0.385 | 0.067 | 5 | 8 | 70 |
| 0.90 | 0.500 | 0.053 | 4 | 4 | 71 |
| 0.92 | 0.667 | 0.027 | 2 | 1 | 73 |
| 0.93 | 1.000 | 0.027 | 2 | 0 | 73 |

These results apply only to that model and corpus. Near-miss questions can score above paraphrases, so inspect your own data before lowering the threshold. Preview with `POST /api/v1/admin/projects/{id}/dedup/preview` returns the top five `similarities`, their scores and whether each meets the threshold; it stores no vector. Run `python -m kebivo.cli dedup-eval --project ID` against labelled project history, or add `--corpus` for the bundled synthetic pairs. The CLI prints precision, recall and counts at thresholds 0.50–0.99. It requires a project with privacy acknowledgement and an enabled embeddings provider. Use group merge or split to correct a mistaken match.

Enabling embeddings queues one backfill for that project. It visits the newest group representatives first, up to `max_scan`, and embeds missing vectors in batches of at most 16. Re-enabling with the same model does not stack jobs. Changing the provider, model or dimensions resets the backfill; vectors for the old configuration are ignored and pruned. Backfill does not regroup stored events. A provider error or missing vector never blocks intake: the new event keeps its exact or own group and routing continues. Failed events are not retried for embedding later.

### Data sent off-host

- **Sent:** only question text, truncated to 2,000 characters. This includes the new question for each processed event, representative questions during backfill (up to `max_scan` historical questions, sent once during a successful pass), and the preview question.
- **Never sent:** answers, corrections, context, tags, metadata, source or user refs, agent or conversation IDs, or group IDs.
- **Received:** vectors only. Vectors are stored locally as derived personal data, deleted with the event and by retention, and never exported or returned by the API.

With a `local` provider on the host or Compose network, nothing leaves the host. A remote endpoint receives the question text described above; selecting embeddings requires an explicit privacy acknowledgement, including for backfill.
