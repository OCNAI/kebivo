---
title: Run Kebivo with Docker
description: Run the published Kebivo image with Docker, create a project token, and send your first report to the inbox.
---

# Run Kebivo with Docker

You need Docker, OpenSSL, Python 3 and a free port 8000. Run the commands in bash; on macOS, type `bash` first, because zsh's `read` doesn't accept `-p` for a prompt. The published image is typically ready in about 12 seconds after download.

**1. Start Kebivo from the published image.** Keep `kebivo.env` private and back it up apart from the data volume.

```sh
(umask 077; printf 'SECRET_KEY=%s\nENCRYPTION_KEY=%s\nALLOWED_ORIGINS=http://127.0.0.1:8000\n' \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > kebivo.env)
docker run -d --name kebivo --restart unless-stopped -p 127.0.0.1:8000:8000 \
  -v kebivo-data:/data --env-file kebivo.env ghcr.io/ocnai/kebivo:0.3.0
curl -fsS --retry 30 --retry-all-errors --retry-delay 1 http://127.0.0.1:8000/health/ready
```

Curl may print a few `Connection reset` errors while migrations run. Readiness returns `{"status":"ok",...}`.

**2. Create an administrator and project token.** The password must have at least 12 characters. The token is issued once into `KEBIVO_TOKEN`; keep it private.

```sh
read -rsp 'Admin password (12+ characters): ' KEBIVO_ADMIN_PASSWORD; printf '\n'
printf '%s\n' "$KEBIVO_ADMIN_PASSWORD" | docker exec -i kebivo python -m kebivo.cli bootstrap --email admin@example.org --password-stdin
unset KEBIVO_ADMIN_PASSWORD
KEBIVO_PROJECT="$(docker exec kebivo python -m kebivo.cli project create --name 'Shop assistant' --json)"
KEBIVO_PROJECT_ID="$(printf '%s' "$KEBIVO_PROJECT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
KEBIVO_TOKEN="$(printf '%s' "$KEBIVO_PROJECT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
```

**3. Report the same unanswered question twice.** Use a different idempotency key for each occurrence. Reuse a key only when retrying the *same* request.

```sh
curl -i http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" \
  -H 'Idempotency-Key: canada-gap-001' \
  --data '{"event_type":"knowledge_gap","question":"Do you ship to Canada?","tags":["shipping"]}'
curl -i http://127.0.0.1:8000/api/v1/feedback \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" \
  -H 'Idempotency-Key: canada-gap-002' \
  --data '{"event_type":"knowledge_gap","question":"Do you ship to Canada?","tags":["shipping"]}'
```

Both responses are `202 Accepted` with distinct `event_id` values. Open <http://127.0.0.1:8000/>, sign in, and find **Do you ship to Canada?** in the Inbox with **2 occurrences**. Use exactly `127.0.0.1`: `localhost` is a different origin, and sign-in names the allowed origin if you use the wrong one. You can also create projects and tokens in the UI.

**4. Route shipping questions to a signed local webhook.** This stands in for the person who owns the shipping docs. Run steps 4 and 5 in the same terminal as step 2: they ask for the administrator password again and reuse `KEBIVO_PROJECT_ID` and `KEBIVO_TOKEN`. The destination returns its signing secret once; keep it private. `allow_private_network: true` lets this destination reach private addresses, so use it only for a trusted receiver.

```sh
read -rsp 'Admin password: ' KEBIVO_ADMIN_PASSWORD; printf '\n'
KEBIVO_COOKIES="$(mktemp)"
KEBIVO_LOGIN="$(KEBIVO_ADMIN_PASSWORD="$KEBIVO_ADMIN_PASSWORD" python3 -c 'import json,os; print(json.dumps({"email":"admin@example.org","password":os.environ["KEBIVO_ADMIN_PASSWORD"]}))' |
  curl -fsS -c "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8000/api/v1/auth/login)"
unset KEBIVO_ADMIN_PASSWORD
KEBIVO_CSRF="$(printf '%s' "$KEBIVO_LOGIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')"
KEBIVO_DESTINATION="$(curl -fsS -b "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H "X-CSRF-Token: $KEBIVO_CSRF" -H 'Content-Type: application/json' \
  --data '{"name":"Shipping docs webhook","type":"webhook","allow_private_network":true,"config":{"url":"http://127.0.0.1:9000/hook","signing":true,"auth":{"type":"none"}}}' \
  http://127.0.0.1:8000/api/v1/destinations)"
KEBIVO_DESTINATION_ID="$(printf '%s' "$KEBIVO_DESTINATION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
WEBHOOK_SECRET="$(printf '%s' "$KEBIVO_DESTINATION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["signing_secret"])')"
```

Get the [stdlib receiver](../examples/webhook_receiver.py) and its [payload schema](webhook-payload.schema.json), then run it in Kebivo's container. This keeps the receiver on container loopback and works with the published image or a Compose build (replace `kebivo` with the Compose app container ID). The last command waits up to 15 seconds for the receiver to start listening, so the delivery doesn't arrive before it.

```sh
mkdir -p examples docs
curl -fsS https://raw.githubusercontent.com/OCNAI/kebivo/v0.3.0/examples/webhook_receiver.py -o examples/webhook_receiver.py
curl -fsS https://raw.githubusercontent.com/OCNAI/kebivo/v0.3.0/docs/webhook-payload.schema.json -o docs/webhook-payload.schema.json
docker cp examples/webhook_receiver.py kebivo:/tmp/webhook_receiver.py
docker cp docs/webhook-payload.schema.json kebivo:/tmp/webhook-payload.schema.json
rm -f kebivo-webhook.log
WEBHOOK_SECRET="$WEBHOOK_SECRET" docker exec -e WEBHOOK_SECRET kebivo python /tmp/webhook_receiver.py \
  --schema /tmp/webhook-payload.schema.json --exit-after 1 > kebivo-webhook.log 2>&1 &
sh -c 'i=0; until grep -q listening kebivo-webhook.log || [ $i -ge 30 ]; do sleep 0.5; i=$((i+1)); done'; cat kebivo-webhook.log
```

The log shows `{"listening": "127.0.0.1:9000"}`. Now add a rule that sends `shipping` reports to the webhook, and report the question a third time.

```sh
curl -fsS -o /dev/null -b "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H "X-CSRF-Token: $KEBIVO_CSRF" -H 'Content-Type: application/json' \
  --data "{\"name\":\"Shipping questions to the docs owner\",\"event_types\":[\"knowledge_gap\"],\"tags\":[\"shipping\"],\"match_mode\":\"any\",\"priority\":1,\"destination_ids\":[\"$KEBIVO_DESTINATION_ID\"],\"match_behavior\":\"stop\"}" \
  "http://127.0.0.1:8000/api/v1/projects/$KEBIVO_PROJECT_ID/routing-rules"
curl -i http://127.0.0.1:8000/api/v1/feedback -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEBIVO_TOKEN" -H 'Idempotency-Key: canada-gap-003' \
  --data '{"event_type":"knowledge_gap","question":"Do you ship to Canada?","tags":["shipping"]}'
```

The report joins the same group, now with 3 occurrences, and the new route delivers it once. By default a destination gets a group's first routed report; later repeats only raise the count. Check the receiver log and the Delivery log:

```sh
sh -c 'i=0; until grep -q "\"signature\": \"valid\"" kebivo-webhook.log || [ $i -ge 20 ]; do sleep 1; i=$((i+1)); done'; sleep 1; cat kebivo-webhook.log
curl -fsS -b "$KEBIVO_COOKIES" "http://127.0.0.1:8000/api/v1/deliveries?destination_id=$KEBIVO_DESTINATION_ID" |
  python3 -c 'import json,sys; [print(d["status"], "attempts=" + str(d["attempt_count"]), d["last_error_category"] or "") for d in json.load(sys.stdin)["items"]]'
```

The receiver log ends with a line containing `"question": "Do you ship to Canada?"`, `"signature": "valid"` and `"schema": "valid"`, and the Delivery log prints `delivered attempts=1 http_2xx`. A retry looks like `retry_scheduled attempts=1 connect`: the receiver wasn't reachable, and Kebivo tries again after about 30 seconds, then 2 and 10 minutes. If you see it, run `cat kebivo-webhook.log`. If the `listening` line is missing, the receiver didn't start: run the receiver command again (from `rm -f kebivo-webhook.log` through the wait), then run the two check commands after a minute. **Delivery log** in the UI shows each attempt.

**5. Resolve the group.** When the shipping docs answer the question, mark the group resolved. In the UI, open **Do you ship to Canada?** in the Inbox and set **Status** to **Resolved** under **Triage**. Or use the API:

```sh
KEBIVO_GROUP_ID="$(curl -fsS -b "$KEBIVO_COOKIES" "http://127.0.0.1:8000/api/v1/admin/groups?project_id=$KEBIVO_PROJECT_ID&tag=shipping" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["id"])')"
curl -fsS -X PATCH -b "$KEBIVO_COOKIES" -H 'Origin: http://127.0.0.1:8000' -H "X-CSRF-Token: $KEBIVO_CSRF" -H 'Content-Type: application/json' \
  --data '{"status":"resolved"}' "http://127.0.0.1:8000/api/v1/admin/groups/$KEBIVO_GROUP_ID" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
rm -f "$KEBIVO_COOKIES" kebivo-webhook.log
unset KEBIVO_LOGIN KEBIVO_DESTINATION KEBIVO_PROJECT WEBHOOK_SECRET KEBIVO_CSRF
```

It prints `resolved`, and the Inbox shows the group as **Resolved** with 3 occurrences. If the question is reported again later, the group reopens and the route sends it to the webhook again.

You can also do the step 4 setup in the UI: sign in, create a signed webhook at **Destinations** with URL `http://127.0.0.1:9000/hook` and **Allow private network**, copy its one-time signing secret, then add a `shipping` rule for it under the project's **Routing** section. Start the receiver with that secret and wait for its `listening` line before sending the third report.

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
