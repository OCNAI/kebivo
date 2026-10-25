---
title: Run Kebivo with Docker
description: Build and run Kebivo locally with Docker Compose, create a project token, and send your first AI knowledge gap report to the inbox.
---

# Run Kebivo with Docker

You need Docker with Compose, OpenSSL and a free port 8000.

**1. Start Kebivo.**

```sh
git clone <REPOSITORY_URL> kebivo
cd kebivo
cp .env.example .env
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
printf 'ALLOWED_ORIGINS=http://127.0.0.1:8000\n' >> .env
docker compose up --build -d
curl -fsS http://127.0.0.1:8000/health/ready
```

Replace `<REPOSITORY_URL>` with the repository address. The first build takes a few minutes. Once migrations finish, readiness returns `{"status":"ok",...}`.

**Or run the published image** instead of cloning and building. It is the same container; keep `kebivo.env` private and back it up apart from the data volume.

```sh
(umask 077; printf 'SECRET_KEY=%s\nENCRYPTION_KEY=%s\nALLOWED_ORIGINS=http://127.0.0.1:8000\n' \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > kebivo.env)
docker run -d --name kebivo --restart unless-stopped -p 127.0.0.1:8000:8000 \
  -v kebivo-data:/data --env-file kebivo.env ghcr.io/ocnai/kebivo:0.1.0
curl -fsS --retry 30 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health/ready
```

With this container, run the step 2 command as `docker exec -it kebivo python -m kebivo.cli bootstrap --email admin@example.org`, and stop it with `docker stop kebivo`; data stays in the `kebivo-data` volume.

**2. Create the administrator.** The command asks for a password (at least 12 characters).

```sh
docker compose exec app python -m kebivo.cli bootstrap --email admin@example.org
```

**3. Create a project and a token.** Open <http://127.0.0.1:8000/> (exactly this address: `localhost` is a different origin and sign-in would fail) and sign in. Create a project, then under **Project tokens** issue a token with the `feedback:gap` scope and copy it. It is shown only once.

**4. Send your first event.**

```sh
export KEBIVO_TOKEN='<PROJECT_TOKEN>'
curl -i http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" \
  -H 'Idempotency-Key: first-event-001' \
  --data '{"event_type":"knowledge_gap","question":"How do I reset my corporate VPN?","tags":["it"]}'
```

The response is `202 Accepted` with an `event_id`. **5. Open the Inbox** in the UI. The question appears in its own group. Add a destination and a routing rule to send future reports to email or a webhook.

To stop: `docker compose down` (data is kept). `docker compose down --volumes` **deletes all data**. For production (TLS, backups, upgrades, every setting), read the [self-hosting guide](self-hosting.md).

Next: [REST API](rest-api.md) or [MCP](mcp.md).
