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
KEBIVO_PROJECT="$(docker exec kebivo python -m kebivo.cli project create --name 'Helpdesk bot' --json)"
KEBIVO_PROJECT_ID="$(printf '%s' "$KEBIVO_PROJECT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
KEBIVO_TOKEN="$(printf '%s' "$KEBIVO_PROJECT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
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

**4. Route it to a signed local webhook.** These commands use the same administrator password and project from step 2. The destination returns its signing secret once; keep it private. `allow_private_network: true` lets this destination reach private addresses, so use it only for a trusted receiver.

```sh
read -rsp 'Admin password: ' KEBIVO_ADMIN_PASSWORD; printf '\n'
KEBIVO_COOKIES="$(mktemp)"
KEBIVO_LOGIN="$(KEBIVO_ADMIN_PASSWORD="$KEBIVO_ADMIN_PASSWORD" python3 -c 'import json,os; print(json.dumps({"email":"admin@example.org","password":os.environ["KEBIVO_ADMIN_PASSWORD"]}))' |
  curl -fsS -c "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8000/api/v1/auth/login)"
unset KEBIVO_ADMIN_PASSWORD
KEBIVO_CSRF="$(printf '%s' "$KEBIVO_LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')"
KEBIVO_DESTINATION="$(curl -fsS -b "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H "X-CSRF-Token: $KEBIVO_CSRF" -H 'Content-Type: application/json' \
  --data '{"name":"Local IT webhook","type":"webhook","allow_private_network":true,"config":{"url":"http://127.0.0.1:9000/hook","signing":true,"auth":{"type":"none"}}}' \
  http://127.0.0.1:8000/api/v1/destinations)"
KEBIVO_DESTINATION_ID="$(printf '%s' "$KEBIVO_DESTINATION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
WEBHOOK_SECRET="$(printf '%s' "$KEBIVO_DESTINATION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["signing_secret"])')"
```

Get the [stdlib receiver](../examples/webhook_receiver.py) and its [payload schema](webhook-payload.schema.json), then run it in Kebivo's container. This keeps the receiver on container loopback and works with the published image or a Compose build (replace `kebivo` with the Compose app container ID).

```sh
mkdir -p examples docs
curl -fsS https://raw.githubusercontent.com/OCNAI/kebivo/v0.1.1/examples/webhook_receiver.py -o examples/webhook_receiver.py
curl -fsS https://raw.githubusercontent.com/OCNAI/kebivo/v0.1.1/docs/webhook-payload.schema.json -o docs/webhook-payload.schema.json
docker cp examples/webhook_receiver.py kebivo:/tmp/webhook_receiver.py
docker cp docs/webhook-payload.schema.json kebivo:/tmp/webhook-payload.schema.json
WEBHOOK_SECRET="$WEBHOOK_SECRET" docker exec -e WEBHOOK_SECRET kebivo python /tmp/webhook_receiver.py \
  --schema /tmp/webhook-payload.schema.json --exit-after 1 > kebivo-webhook.log 2>&1 &
```

Add a rule for `it`, then send a new question. The receiver writes a JSON line with `"signature":"valid"` and `"schema":"valid"` to `kebivo-webhook.log`.

```sh
curl -fsS -b "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H "X-CSRF-Token: $KEBIVO_CSRF" -H 'Content-Type: application/json' \
  --data "{\"name\":\"Route IT locally\",\"event_types\":[\"knowledge_gap\"],\"tags\":[\"it\"],\"match_mode\":\"any\",\"priority\":1,\"destination_ids\":[\"$KEBIVO_DESTINATION_ID\"],\"match_behavior\":\"stop\"}" \
  "http://127.0.0.1:8000/api/v1/projects/$KEBIVO_PROJECT_ID/routing-rules"
curl -i http://127.0.0.1:8000/api/v1/feedback -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" -H 'Idempotency-Key: routed-event-001' \
  --data '{"event_type":"knowledge_gap","question":"Where is the IT VPN reset guide?","tags":["it"]}'
rm -f "$KEBIVO_COOKIES"
unset KEBIVO_LOGIN KEBIVO_DESTINATION KEBIVO_PROJECT WEBHOOK_SECRET KEBIVO_CSRF
tail -f kebivo-webhook.log
```

You can also do the setup in the UI: sign in, create a signed webhook at **Destinations** with URL `http://127.0.0.1:9000/hook` and **Allow private network**, copy its one-time signing secret, then add an `it` rule for it under the project's **Routing** section. Start the receiver with that secret before sending the second event. Check **Delivery log** if the receiver does not print a delivery.

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
