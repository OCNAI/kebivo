---
title: Self-hosting Kebivo
description: Run Kebivo in one Docker container with SQLite. Configure TLS, secrets, backups, upgrades and local storage for a self-hosted feedback inbox.
---

# Self-hosting guide

Kebivo runs in one container with one SQLite file. The container serves the API, admin UI and MCP endpoint alongside a background worker. It applies database migrations before accepting traffic. The default install needs no external database, queue or account and makes no outbound connection.

## Requirements

- Docker Engine with the Compose plugin (`docker compose`). The image is built locally from this repository; no image is published yet.
- Free disk for the image build and the data volume. The SQLite file grows with your feedback.
- OpenSSL (or another source of random hex) to generate keys.
- A free local port, 8000 by default.
- **Local storage** for the data volume. Network filesystems (NFS, SMB) and running several app replicas on one database are not supported.

## Install

```sh
git clone <REPOSITORY_URL> kebivo
cd kebivo
cp .env.example .env
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ALLOWED_ORIGINS=http://127.0.0.1:8000\n' >> .env
docker compose up --build -d
curl -fsS http://127.0.0.1:8000/health/ready
docker compose exec app python -m kebivo.cli bootstrap --email admin@example.org
```

`<REPOSITORY_URL>` is the address you cloned from. Readiness returns `"status":"ok"` with `"database":"ok","search":"ok","worker":"ok"` once migrations are done. The last command prompts for the administrator password. Then open <http://127.0.0.1:8000/> and sign in. The [README quickstart](../README.md#quickstart) continues from here to the first feedback event.

Compose runs only the `app` service. It builds the image `kebivo:local`, stores SQLite at `/data/app.sqlite3` in the named volume `app-data`, publishes the port on `127.0.0.1` only, probes `/health/ready` every 30 seconds, and restarts the container unless you stop it. The container runs as an unprivileged user (UID 10001).

| Command | Effect |
| --- | --- |
| `docker compose logs -f app` | Follow the logs. They contain request metadata and error categories, never feedback content or credentials. |
| `docker compose stop` / `docker compose up -d` | Stop and start; data is kept. |
| `docker compose down` | Remove the container; the volume and data are kept. |
| `docker compose down --volumes` | **Deletes the volume and all feedback.** |

## Configuration

Settings come from environment variables. Compose reads them from `.env`, which you copy from [`.env.example`](../.env.example). Every value is validated at startup: an invalid or missing required value stops the container with a message that names the variable (never its value). The application never generates a replacement production key.

### Required

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | At least 32 characters, not a known placeholder. Keys project-token digests and login-throttle identifiers. Keep it stable: changing it invalidates **every project token**. It does not encrypt data. |
| `ENCRYPTION_KEY` | 64 hex characters (`openssl rand -hex 32`). Encrypts destination secrets and LLM provider keys with AES-256-GCM. Store it separately from database backups. Losing it means re-entering those secrets. |
| `ALLOWED_ORIGINS` | Exact, comma-separated browser origins that may sign in, such as `https://feedback.example.org` (scheme, host and port, no path). In production only loopback origins may use HTTP. Empty means no browser can sign in. `localhost` and `127.0.0.1` are different origins. |
| `PUBLIC_URL` | Optional HTTPS origin of this Kebivo instance. Slack destinations include a link to the feedback group when set. |

A wrong-origin 403 lists the configured `ALLOWED_ORIGINS` by design.

### Network and access

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_BIND_ADDRESS`, `APP_PORT` | `127.0.0.1`, `8000` | Compose only: the host address and port. Keep loopback behind a reverse proxy. |
| `TRUSTED_PROXY_IPS` | Empty | Comma-separated proxy peer IPs whose `X-Forwarded-For` is used for client-IP rate limits. Forwarded headers from other peers are ignored. |
| `MCP_ENABLED` | `true` | `false` removes `/mcp` at startup. |
| `MCP_ALLOWED_HOSTS` | Empty | Extra `host` or `host:port` values accepted in the MCP `Host` header. Loopback and hosts from `ALLOWED_ORIGINS` are always allowed. |

### Limits

| Variable | Default | Purpose |
| --- | --- | --- |
| `FEEDBACK_RATE_LIMIT_PER_TOKEN` | `120` | Submissions per token per minute, shared by REST and MCP. |
| `FEEDBACK_RATE_LIMIT_PER_IP` | `300` | Feedback and MCP requests per client IP per minute. Counters are in memory and reset on restart. |
| `IDEMPOTENCY_WINDOW_HOURS` | `24` | How long an idempotency key replays its original event (1–720). |

Request body limits (131,072 bytes for feedback and MCP, 65,536 bytes for `/api/v1/auth/*`, 1 MiB for other `POST`, `PUT` and `PATCH` routes under `/api/v1/`; larger bodies get `413 payload_too_large` before they are read) and the private-network policy for webhooks and LLM providers are fixed in code. Private targets are opted into per destination or provider, not globally.

### Worker and delivery

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKER_ENABLED` | `true` | `false` pauses routing and delivery; ingestion continues and work waits. |
| `WORKER_CONCURRENCY` | `4` | Simultaneous delivery calls (1–32). |
| `WORKER_POLL_INTERVAL_SECONDS` | `1` | Seconds between worker claims (1–60). |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `20` | Time for in-flight sends at shutdown (1–120). Compose waits 30 seconds before it kills the container. |
| `DELIVERY_RETRY_SCHEDULE_SECONDS` | `30,120,600,1800,7200` | Retry delays with ±20% jitter; up to six automatic attempts. |
| `DELIVERY_RETRY_AFTER_MAX_SECONDS` | `7200` | Largest `Retry-After` from a receiver that is honoured. |
| `LLM_MAX_CONCURRENCY` | `2` | Simultaneous semantic-deduplication model calls (1–8). Add it to `compose.yaml` if you change it. |

### Retention

| Variable | Default | Deletes |
| --- | --- | --- |
| `FEEDBACK_RETENTION_DAYS` | `180` | Feedback events older than this, with their deliveries and search rows. |
| `DELIVERY_LOG_RETENTION_DAYS` | `30` | Attempt details of finished deliveries. The delivery and its status stay. |
| `AUDIT_RETENTION_DAYS` | `90` | Audit entries. |

Values are whole days from 1 to 36500; `0` turns off automatic deletion for that class. Retention is **on by default**. The worker runs it once a day. See [retention](retention.md) for the schedule, the CLI and what is kept.

### Other

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENCRYPTION_KEY_PREVIOUS` | Empty | The old key, only while [rotating](#rotate-credentials) `ENCRYPTION_KEY`. |
| `LLM_API_KEY_*` | None | Optional provider keys referenced by name from an LLM provider (`api_key_env`). Compose does not pass them by default: add them under `environment:` in a `compose.override.yaml`. See [LLM providers](llm-providers.md). |
| `APP_ENV` | `production` in the image | `development`, `test` or `production`. Production enforces the key and origin rules. |
| `DATABASE_URL` | `sqlite:////data/app.sqlite3` | Must be a SQLite URL. Compose fixes it to the volume. |
| `DATABASE_PATH` | From `DATABASE_URL` | Optional override of the SQLite file path. |
| `DISPLAY_NAME` | `Kebivo` | Backend display name (the OpenAPI title). The UI name comes from `config/project.json` at build time. |

Compose passes only the variables listed in `compose.yaml`. Restart the container after changing `.env`: `docker compose up -d`.

## Administrator access

There is exactly one administrator and no public signup. Create it from a trusted shell. The CLI reads the password twice without echo and requires at least 12 characters. Never pass passwords as arguments or environment values.

```sh
docker compose exec app python -m kebivo.cli bootstrap --email admin@example.org
```

Sessions last eight hours with a 30-minute idle timeout, use an `HttpOnly`, `SameSite=Lax` cookie (Secure over HTTPS), and store only a digest of the session token. Five failed logins within 15 minutes, for the same e-mail or from the same IP address, return `429` even for the correct password. Cookie-authenticated writes need the configured `Origin` and the session's `X-CSRF-Token`.

To reset a lost password, or to get back in while failed attempts from someone else keep the login throttled (this signs out every session and clears the login throttle):

```sh
docker compose exec app python -m kebivo.cli reset-password --email admin@example.org
```

## Reverse proxy and TLS

For access from other machines, terminate TLS at a reverse proxy and keep the app port bound to loopback. Plain HTTP is only for loopback development; never publish the app port directly. An Nginx example with an existing certificate:

```nginx
server {
    listen 443 ssl;
    server_name feedback.example.org;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

Then:

- Redirect HTTP to HTTPS.
- Set `ALLOWED_ORIGINS=https://feedback.example.org`.
- Set `TRUSTED_PROXY_IPS` to the proxy's peer address as the container sees it. With the proxy on the host and Docker's default bridge this is the bridge gateway, for example `172.17.0.1` (see `docker network inspect` for the Compose network's gateway).
- Make the proxy **replace** `X-Forwarded-For` with the client address, as above.
- Keep the `Authorization` header and the original `Host` for `/mcp`. Because the host in `ALLOWED_ORIGINS` is already allowed for MCP, this setup needs no `MCP_ALLOWED_HOSTS`. If the public URL has a non-default port, include it.

Caddy, Traefik or a cloud load balancer work the same way: TLS at the edge, the original `Host`, a replaced `X-Forwarded-For`, and the proxy in `TRUSTED_PROXY_IPS`.

## Secrets and what to protect

| Where | What it holds | Protect it |
| --- | --- | --- |
| `.env` | `SECRET_KEY`, `ENCRYPTION_KEY`, `ENCRYPTION_KEY_PREVIOUS`, any `LLM_API_KEY_*` | Readable only by the operator (`chmod 600 .env`). Never commit it. |
| Volume `app-data`: `/data/app.sqlite3` and its `-wal` and `-shm` files | **Feedback content in plain text**, admin password hash (Argon2), project-token and session digests, destination and LLM secrets encrypted with `ENCRYPTION_KEY`, audit history | Local disk with restricted access. |
| Backups of the database | Everything in the database | Store off the host with restricted access and your own retention period. |

Feedback is not encrypted at rest. Use disk or volume encryption if your policy needs it. Back up `.env` (or at least `SECRET_KEY` and `ENCRYPTION_KEY`) separately from the database backups. Without the matching keys, a database backup loses every project token and every stored destination and provider secret.

## Backup and restore

Back up while the app runs. The command uses SQLite's online backup API, checks the copy's integrity and migration version, and streams a single self-contained file to the host. **Do not copy `/data/app.sqlite3` alone while the app runs:** the WAL file may hold committed changes.

```sh
umask 077
mkdir -p backups
backup_file="backups/app-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
if docker compose exec -T app python -m scripts.sqlite_maintenance backup > "$backup_file"; then
  echo "Saved $backup_file"
else
  rm -f "$backup_file"
  echo "Backup failed" >&2
fi
```

Run it on a schedule (for example from cron) and copy the files off the host.

To restore, first take a fresh backup of the current database, then stop the app. The restore validates the input before it replaces anything, checkpoints the old WAL and removes stale WAL and SHM files. It runs in a one-off container on the same volume; the app must not run meanwhile.

```sh
BACKUP_FILE=backups/app-YYYYMMDDTHHMMSSZ.sqlite3
docker compose stop app
docker compose run --rm --no-deps -T --entrypoint python app -m scripts.sqlite_maintenance restore < "$BACKUP_FILE"
docker compose up -d app
curl -fsS http://127.0.0.1:8000/health/ready
```

Restore the matching `.env` before `docker compose up -d app`. At startup, pending migrations upgrade an older backup; a backup from a newer, unknown version stops startup. Keep the pre-restore backup until you have checked the UI and the health endpoint.

**Retention and backups.** Retention and manual deletion remove data from the live database (SQLite secure delete, then a WAL checkpoint), but **not from backups taken earlier**. Restoring an old backup brings deleted feedback back until the next retention run removes it again. Expire old backups according to your own policy.

## Upgrades

Migrations are versioned and run automatically before the server starts. A failed migration stops startup and leaves the previous data untouched.

1. Read [`CHANGELOG.md`](../CHANGELOG.md) for configuration changes.
2. Take a backup (above) and keep `.env`.
3. Update the source and rebuild:

   ```sh
   git pull
   docker compose up --build -d
   curl -fsS http://127.0.0.1:8000/health/ready
   ```

4. Check the version in **Settings** or with `docker compose exec app python -m kebivo.cli version`.

The data volume survives the rebuild. To go back, restore the backup you took and check out the matching source; migration downgrades can drop data and are not an upgrade path. Release maintainers verify each version with an image-to-image upgrade check on a populated volume (see [releasing](releasing.md)).

## Rotate credentials

| Credential | How |
| --- | --- |
| **Project token** (leaked or no longer needed) | Revoke it in the admin UI (project → **Project tokens** → **Revoke**) or with `POST /api/v1/projects/{id}/tokens/{token_id}/revoke`. It stops working on the next request. Issue a new token and update the client first if you need continuity. Give each client its own token so you can revoke one without affecting others. |
| **Admin password** | `python -m kebivo.cli reset-password --email ...` (above). Every session is revoked. |
| **Webhook signing secret** | **Rotate** on the destination, or `POST /api/v1/destinations/{id}/rotate-signing-secret`. The new secret is shown once; update the receiver. |
| **Webhook headers, SMTP password, LLM API key** | Enter the new value in the destination or LLM provider form. Values are write-only and never shown again. |
| **`ENCRYPTION_KEY`** | Put the current key in `ENCRYPTION_KEY_PREVIOUS`, set a new `ENCRYPTION_KEY`, stop the app, run `docker compose run --rm --no-deps app python -m kebivo.cli reencrypt-secrets`, start the app, then remove `ENCRYPTION_KEY_PREVIOUS`. Startup refuses secrets that no configured key can decrypt. |
| **`SECRET_KEY`** | Changing it invalidates every project token and resets login throttling. Set the new key, restart, then issue new tokens to every client. Only do this if the key leaked. |

If the host or a backup may be compromised, rotate all of the above: tokens, admin password, both keys and every destination and provider secret.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Container exits at startup | `docker compose logs app`. The message names the invalid variable (for example a short `SECRET_KEY` or missing `ENCRYPTION_KEY`) or the failed migration. |
| `docker compose up` says `Set SECRET_KEY ...` | The variable is missing from `.env`. |
| Sign-in fails with a forbidden error | The URL in the browser must match an entry in `ALLOWED_ORIGINS` exactly, including scheme and port. |
| `/health/ready` returns `503` | `"database":"unavailable"`: the volume or file is not readable. `"search":"unavailable"`: the search index failed to start. `"worker":"stopped"`: the worker crashed; check the logs and restart. `"worker":"disabled"` with status `ok` only means `WORKER_ENABLED=false`. |
| `/health/ready` shows a rising `routing_backlog.pending_count` | Routing jobs are waiting. `oldest_pending_age_seconds` shows how long the oldest has waited. A growing queue with `worker: ok` means the worker is running but intake exceeds its processing rate; a stopped worker returns 503. Queued work alone does not fail readiness. |
| Port 8000 is in use | Set `APP_PORT` in `.env` and use that port in your URLs and `ALLOWED_ORIGINS`. |
| Rate limits hit everyone at once behind a proxy | `TRUSTED_PROXY_IPS` is not set, so every request appears to come from the proxy. |
| MCP returns `421` | The `Host` the client uses is not in `ALLOWED_ORIGINS` or `MCP_ALLOWED_HOSTS`, or the proxy does not forward the original `Host`. |
| A webhook or provider is rejected as unsafe | Private, loopback and metadata addresses are blocked unless the destination or provider opts into private networks; metadata addresses are always blocked. |
| Deliveries stay `retry_scheduled` | Open `/deliveries` for the attempt error. Fix the receiver; retries continue on schedule, or use **Retry** after a final failure. |
| `database is locked` or corruption on a network share | Move the volume to local storage. |
| Group counts look wrong | `docker compose exec app python -m kebivo.cli check-groups`. |

Report security problems as described in [`SECURITY.md`](../SECURITY.md), not in a public issue.

## Intake capacity check

The 0.2.0 one-host observation used 4 CPU cores, 7.6 GiB RAM, 10,000 reports, 8 concurrent writers, and a load generator on the same host. It saw 9 SQLite lock HTTP 500s, p95 response time 1.1 seconds, and a 5.2-second maximum. A 12,000-job routing queue drained at roughly 24–30 jobs per second. These are observations from one host, not capacity guarantees. Keep the database on local storage and watch `routing_backlog` during bursts. Increase host capacity or reduce intake if the pending age keeps growing; SQLite still has one writer, and a sustained overload may return retryable 503s.

To reproduce a burst on a disposable instance with a project token, set `FEEDBACK_RATE_LIMIT_PER_TOKEN=20000` and `FEEDBACK_RATE_LIMIT_PER_IP=20000` before starting the server. Point the script at the same SQLite file used by that server; do not run it against production feedback:

```sh
export KEBIVO_BURST_TOKEN='<disposable project token>'
python3 scripts/feedback-burst.py --url http://127.0.0.1:8000 --database /path/to/app.sqlite3 --events 10000 --writers 8
curl -fsS http://127.0.0.1:8000/health/ready
```

The script prints counts by HTTP status (including 202, 429, and 503), persisted event count, p95, and maximum latency. It exits with an error if 202 responses do not match persisted events or if any HTTP 500 occurs. Use the same settings and host for before/after comparisons; network and disk differences change the result.
