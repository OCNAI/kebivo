# Retention

Retention runs as a separate task in the embedded worker. Deliveries, routing and duplicate checks continue while it runs. Only one retention run can be active at a time. The first check is five minutes after startup, then every ten minutes. At least 24 hours pass between completed automatic runs. Start a manual run with `python -m kebivo.cli retention --run`; `--dry-run` prints estimated counts without writing. Kebivo retains the last 30 runs, and the admin settings API shows the latest five.

| Environment variable | Default | Removed data |
| --- | ---: | --- |
| `FEEDBACK_RETENTION_DAYS` | 180 | Feedback events created before the cutoff, including associated deliveries and search rows |
| `DELIVERY_LOG_RETENTION_DAYS` | 30 | Attempts for delivered or failed deliveries, using completion time or start time |
| `AUDIT_RETENTION_DAYS` | 90 | Audit events created before the cutoff |

Values are whole days from 1 to 36500. `0` disables automatic deletion for that class. The cutoff is strict: a row exactly at the cutoff stays. Settings are read only in the UI and must be changed through the environment.

Feedback deletion uses the same group, representative, occurrence and search maintenance as manual deletion. Events with a processing job or delivery are skipped until a later run (`skipped_busy`). An event without a feedback group is kept and counted as `skipped_error` instead of stopping the run. Pending and scheduled deliveries of an expired event are removed with it. A delivery whose attempt details were pruned retains its status and attempt count; later retries continue numbering. Expired admin sessions and idempotency records, plus login attempts older than 15 minutes, are removed regardless of the three settings.

Each feedback batch holds at most 25 events; row batches hold at most 500 rows, with a 0.1 second pause between batches so ingestion and delivery can write. A crash leaves completed batches committed and the next run continues. On shutdown the run stops at the next batch boundary and is recorded as failed with cause `RetentionCancelled`; the next check starts a new run. A completed run writes one audit entry with cutoffs and counts only. The worker checkpoints the WAL after feedback deletion. The database uses SQLite secure deletion, but **backups and snapshots taken before a run retain their old content**. Apply your backup retention policy separately and store backups securely.
