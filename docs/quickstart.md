---
title: Run Kebivo with Docker
description: Build and run Kebivo locally with Docker Compose, create a project token, and send your first AI knowledge gap report to the inbox.
---

# Run Kebivo with Docker

You need Docker, OpenSSL, Python 3 and a free port 8000. The published image is typically ready in about 12 seconds after download.

**1. Start Kebivo from the published image.** Keep `kebivo.env` private and back it up apart from the data volume.

```sh
(umask 077; printf 'SECRET_KEY=%s\nENCRYPTION_KEY=%s\nALLOWED_ORIGINS=http://127.0.0.1:8000\n' \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > kebivo.env)
docker run -d --name kebivo --restart unless-stopped -p 127.0.0.1:8000:8000 \
  -v kebivo-data:/data --env-file kebivo.env ghcr.io/ocnai/kebivo:0.1.1
curl -fsS --retry 30 --retry-all-errors --retry-delay 1 http://127.0.0.1:8000/health/ready
```

Curl may print a few `Connection reset` errors while migrations run. Readiness returns `{"status":"ok",...}`.

**2. Create an administrator and project token.** The password must have at least 12 characters. The token is issued once into `KEBIVO_TOKEN`; keep it private.

```sh
read -rsp 'Admin password (12+ characters): ' KEBIVO_ADMIN_PASSWORD; printf '\n'
printf '%s\n' "$KEBIVO_ADMIN_PASSWORD" | docker exec -i kebivo python -m kebivo.cli bootstrap --email admin@example.org --password-stdin
unset KEBIVO_ADMIN_PASSWORD
KEBIVO_TOKEN="$(docker exec kebivo python -m kebivo.cli project create --name 'Helpdesk bot' --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
```

**3. Send your first event.**

```sh
curl -i http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" \
  -H 'Idempotency-Key: first-event-001' \
  --data '{"event_type":"knowledge_gap","question":"How do I reset my corporate VPN?","tags":["it"]}'
```

The response is `202 Accepted` with an `event_id`. Open <http://127.0.0.1:8000/> to sign in and see the report in the Inbox. Use exactly `127.0.0.1`: `localhost` is a different origin, and sign-in names the allowed origin if you use the wrong one. You can also create projects and tokens in the UI.

**Build from source instead.** Use this path if you want to change the code. After it starts, replace both `docker exec -i kebivo` and `docker exec kebivo` with `docker compose exec -T app` in step 2.

```sh
git clone https://github.com/OCNAI/kebivo.git
cd kebivo
cp .env.example .env
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ALLOWED_ORIGINS=http://127.0.0.1:8000\n' >> .env
docker compose up --build -d
curl -fsS --retry 30 --retry-all-errors --retry-delay 1 http://127.0.0.1:8000/health/ready
```

To stop the published image: `docker stop kebivo` (data stays in the volume). To stop a Compose build: `docker compose down` (data is kept). `docker compose down --volumes` **deletes all data**. For production (TLS, backups, upgrades, every setting), read the [self-hosting guide](self-hosting.md).

Next: [REST API](rest-api.md) or [MCP](mcp.md).
