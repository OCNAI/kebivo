---
title: Feedback inbox and triage
description: Search and triage Kebivo knowledge gap groups in the admin inbox with statuses, notes, tags, merge, split and deletion.
---

# Feedback management

REST and MCP submissions commit the event, its group membership, history and a processing job together. Exact matching can join an existing group. An idempotent replay returns the original event without creating another group. The admin event list includes `group_id`; event detail includes the group's ID and status.

Each event belongs to exactly one group. Groups store a representative event, status, occurrence count, and first and last seen times. The count and times describe member events. Tags and deliveries remain on events. Group notes and history are separate rows; history records IDs and actions, never feedback or note text. Search is indexed by SQLite FTS5 over event question, correction, previous answer and context. SQLite secure deletion is enabled for the FTS index and database connections. Migration 0008 requires SQLite 3.42 or newer with FTS5 and checks both before changing persistent tables.

The status values are `open`, `in_progress`, `resolved` and `dismissed`. New groups start `open`; an administrator can move a group between any two statuses. Entering `resolved` sets `resolved_at`, and leaving it clears that field. Setting the current status again makes no history entry. An exact occurrence reopens a resolved group and records `triggered_reopen`; a dismissed group stays dismissed. Manual merge does not count as a new occurrence. Group event items show the match method and reopen marker. See [deduplication](deduplication.md).

Run `python -m kebivo.cli check-groups` against the configured database to verify membership and derived fields. It exits with code 1 and prints each violation when an event has no group, a group has no members, count or seen times differ, the representative is not a member, or member project/type differs from the group. A clean run prints `Feedback groups are consistent.`

Migration `0008_feedback_management` backfills one group and history entry per existing event and indexes all existing feedback. Downgrading to 0007 removes groups, notes, history, admin tag provenance and the search index. Back up the SQLite database before a downgrade; that data cannot be recovered from the downgraded database.

## Group read API

Administrators can browse `GET /api/v1/admin/groups` with a cookie session. Project bearer tokens receive 401. Responses are `no-store`; the list has no total count. Each row includes its project, type, status, representative event and a question excerpt of at most 280 characters, occurrence count, first and last seen times, the first 20 combined tags (`tags_more` flags any remainder), and counts of delivered, processing (`in_progress`) and failed deliveries. Pending and scheduled retries do not appear in those three counts.

The list accepts these query parameters:

| Parameter | Meaning |
| --- | --- |
| `project_id`, `event_type` | Exact project UUID and `knowledge_gap` or `knowledge_correction`. |
| `status` | Repeatable; any of `open`, `in_progress`, `resolved`, `dismissed`; matches any listed status. |
| `tag` | Repeatable tag name or slug, normalized with `normalize_tag_v1`; up to 10 values. The group's combined tags must contain **all** values. Invalid tags return 422. |
| `q` | Search member questions, corrections, previous answers and context through FTS5. |
| `first_seen_from`, `first_seen_to`, `last_seen_from`, `last_seen_to` | Inclusive ISO 8601 date/time bounds, normalized to UTC. Values without an offset are treated as UTC. |
| `min_occurrences` | Minimum member count, at least 1. |
| `sort` | `last_activity_desc` (default), `last_activity_asc`, `occurrences_desc`, `occurrences_asc`, `status`, `first_reported_desc`. |
| `limit`, `cursor` | Page size 1–100 (default 50), then the returned opaque `next_cursor`. |

Search splits input into Unicode word tokens, quotes them as literals, joins them with AND, and treats the final token as a prefix. For example `reset pass` matches a member containing `reset` and a word beginning with `pass`. FTS operators and punctuation do not change query syntax. Queries with no word tokens return 422; `q` is limited to 200 characters. Search matches a group when **any** member matches. A cursor is bound to the filters and sort; reusing it with another query returns 400 `invalid_cursor`.

Every sort has a stable group-ID tie break. `status` sorts open, in progress, resolved, dismissed, then newest activity within each status. Occurrence sorts use newest activity as a secondary key. Paging uses keyset cursors, so new groups can appear ahead of an existing page while browsing.

`GET /api/v1/admin/groups/summary` accepts `project_id` and returns total groups, total events, counts by status, failed deliveries updated in the last 24 hours, and the five most frequent open knowledge-gap groups. `GET /api/v1/admin/groups/{id}` returns the full representative question, all combined tags with counts by provenance, newest-first notes and the delivery summary. `/{id}/events` returns full event detail, including `from_admin` tag provenance, 20 oldest-first per page. `/{id}/history` returns 50 newest-first history rows per page. `GET /api/v1/deliveries?group_id=<uuid>` filters the existing delivery log to the group's member events.

## Group triage API

All writes below require an administrator cookie, the allowed `Origin` and `X-CSRF-Token`. Project bearer tokens receive 401. `PATCH /api/v1/admin/groups/{id}` accepts `status` and/or `representative_event_id`. A representative must be an event in that group; arbitrary text cannot be used. A change to both fields writes separate history and audit entries. A repeated status or representative choice is a 200 no-op.

`POST /api/v1/admin/groups/{id}/notes` accepts `{ "body": "..." }` and returns the note with its ID, author and creation time. Notes are administrator plain text, 1–4,000 characters, at most 500 per group. `DELETE /api/v1/admin/groups/{id}/notes/{note_id}` removes one note. Notes cannot be edited. The note body is stored only in the note row; the add and delete trails contain its ID only.

`PATCH /api/v1/admin/groups/{id}/tags` accepts `{ "add": ["..."], "remove": ["..."] }`. Names are normalized with `normalize_tag_v1`, and newly created tags belong to the group's project. Adding marks each member's tag link as `from_admin`; removing deletes every member link for that slug, including submitted and default links. A history entry records the removed slug and counts of its prior default, submission and admin provenance. An edit that would leave any member with more than 30 tags returns 422 without changing the group. Tag edits on groups with more than 10,000 members return 422 `tag_edit_limit`; no tags change. Tag edits do not re-route or re-send deliveries.

`feedback_group_history` powers the group detail view and is deleted with its group. `audit_events` is the separate security trail retained under the audit policy. Every effective manual change writes both in the same transaction. Their details contain IDs, statuses, tag slugs and provenance counts; they never contain questions, corrections, context, notes or search text.

## Merge, split and delete

`POST /api/v1/admin/groups/{id}/merge` takes `{"source_group_ids":["..."]}` with 1–20 distinct source groups. All groups must belong to the same project and have the same event type. A mismatch returns 422 `merge_incompatible` without changing membership. A merge that would put more than 500 notes in the target returns 422 `note_limit`. The target keeps its status and representative. Members move with `manual_merge`; notes and history move to the target, and moved history records its former `origin_group_id`. The source groups are removed, the target's count and seen times are recomputed, and a `merged` history entry and audit event are written.

`POST /api/v1/admin/groups/{id}/split` takes `{"event_ids":["..."],"representative_event_id":"..."}`. Supply 1–500 distinct members; the representative is optional and defaults to the first selected event. At least one event must remain in the source (`split_all_members` otherwise). The selected members move to a new `open` group with `manual_split`. The source keeps its status and gets a replacement representative when needed. Both groups' counts and seen times are recomputed, with `split_out`, `split_in` and audit entries. Notes and earlier history remain with the source.

`DELETE /api/v1/admin/feedback/{event_id}` permanently removes one event and returns `{ "group_id": "...", "group_deleted": true|false }`. A processing job or delivery returns 409 `feedback_busy`; retry when it finishes. The deletion removes event tags, routing matches, all deliveries and attempts, the processing job, idempotency record and search index entry. It cancels pending deliveries. If members remain, the group gets a replacement representative if needed, recomputed counts and seen times, and `event_deleted` history. Deleting the last member also removes the group, its notes and its history. Audit details contain IDs and counts only. Secure deletion clears the main database file; the post-commit WAL checkpoint is best effort. It returns busy while another connection holds a read, so deleted text can remain in the `-wal` file until the next successful checkpoint. A checkpoint failure is logged without feedback text and does not undo the committed deletion. Previously sent copies at destinations cannot be recalled, and existing backups still contain the data until those backups expire or are removed.

## Admin UI

The `/inbox` page uses the group list API. Date pickers use UTC day boundaries. Search, project, type, repeated status and tag filters, inclusive first/last-seen bounds, minimum occurrences and sort are kept in the query string so a URL can be reloaded or shared. Status and tag chips remove one filter at a time. “Load more” follows the opaque cursor and avoids appending duplicate group IDs. The page provides loading, empty and retryable error states. The home dashboard shows group/event totals, per-status counts, recent failed deliveries, and the API's top recurring open gaps; status and open-gap links open filtered inbox views. Both screens use same-origin session-cookie requests.

`/inbox/groups/{id}` shows paged full-text occurrences, group history and delivery history. Each occurrence separates **Reported by agent**, **Submitted correction (unverified)**, **Administrator notes**, and **System** content. Text and metadata render as literal text. Administrators can change status, select a representative occurrence, add or delete notes, and add or remove tags; tag edits do not re-route. Merge searches only groups in the same project and event type, warns that resolved or dismissed targets retain their status, and reports incompatible-group errors. Split supports a chosen representative and blocks moving every occurrence. Delete confirmation explains that delivery records and, for the last occurrence, the group and notes will be removed; it also explains that sent copies cannot be recalled. A busy deletion says “being delivered, try again shortly”; deleting the last occurrence returns to the inbox. Dialogs return keyboard focus to the control that opened them. Delivery attempts open in a dialog, and Retry is offered only for failed deliveries. Legacy `/inbox/{event_id}` URLs resolve to the event's group and highlight that occurrence.

`make smoke` verifies the built container against these behaviors. It covers group creation at ingest, the open/resolved `it` filters after an app restart, group authorization, merge/split membership and occurrence preservation, delivered and retry-scheduled deletion, audit redaction, canary search and log sweeps, and `check-groups` totals. It also migrates a database copy from 0007 to 0008 and records a 10,000-event search latency sample; latency is informational, not a gate.
