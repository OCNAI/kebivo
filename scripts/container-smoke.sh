#!/usr/bin/env bash
set -euo pipefail

project="kebivo-smoke-${RANDOM}-${RANDOM}"
port="${APP_PORT:-18080}"
export APP_PORT="$port"
export ALLOWED_ORIGINS="http://127.0.0.1:${port}"
export SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"
export ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
export DELIVERY_RETRY_SCHEDULE_SECONDS="${DELIVERY_RETRY_SCHEDULE_SECONDS:-1,1,1,1,1}"
compose=(docker compose -p "$project" -f compose.yaml -f compose.smoke.yaml -f compose.phase6-smoke.yaml)
cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" up --build -d

ready=0
for attempt in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${port}/health/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  "${compose[@]}" logs
  echo "application did not become ready" >&2
  exit 1
fi

# Verify Compose passes overrides into a fresh app container, including the
# MCP switch that controls whether /mcp is installed at startup.
FEEDBACK_RATE_LIMIT_PER_TOKEN=17 FEEDBACK_RATE_LIMIT_PER_IP=19 \
  IDEMPOTENCY_WINDOW_HOURS=6 MCP_ENABLED=false \
  MCP_ALLOWED_HOSTS=smoke.example.invalid:8443 \
  "${compose[@]}" run --rm --no-deps -T --entrypoint python app - <<'PY'
from kebivo.app import app

settings = app.state.settings
assert settings.feedback_rate_limit_per_token == 17
assert settings.feedback_rate_limit_per_ip == 19
assert settings.idempotency_window_hours == 6
assert settings.mcp_enabled is False
assert settings.mcp_allowed_hosts == ("smoke.example.invalid:8443",)
assert all(getattr(route, "path", None) != "/mcp" for route in app.routes)
PY

curl --fail --silent "http://127.0.0.1:${port}/health/live" | grep -q '"status":"ok"'
curl --fail --silent "http://127.0.0.1:${port}/health/ready" | grep -q '"worker":"ok"'
curl --fail --silent "http://127.0.0.1:${port}/" | grep -q '<div id="root">'

# Bootstrap through the local-only container CLI and exercise login/CSRF/logout.
printf 'smoke-admin-password-123\nsmoke-admin-password-123\n' | \
  "${compose[@]}" exec -T app python -m kebivo.cli bootstrap \
    --email smoke-admin@example.invalid --password-stdin
cookie_jar="$(mktemp)"
trap 'rm -f "$cookie_jar"; cleanup' EXIT
login_body="$(curl --fail --silent --show-error -c "$cookie_jar" \
  -H "Origin: http://127.0.0.1:${port}" -H 'Content-Type: application/json' \
  --data '{"email":"smoke-admin@example.invalid","password":"smoke-admin-password-123"}' \
  "http://127.0.0.1:${port}/api/v1/auth/login")"
csrf_token="$(printf '%s' "$login_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')"

api="http://127.0.0.1:${port}/api/v1"
write_headers=(-H "Origin: http://127.0.0.1:${port}" -H "X-CSRF-Token: ${csrf_token}")
"${compose[@]}" exec -T app sh -c 'cat > /tmp/smoke-cookies' < "$cookie_jar"
"${compose[@]}" exec -T app sh -c 'cat > /tmp/webhook-payload.schema.json' < docs/webhook-payload.schema.json
phase4_evidence="$("${compose[@]}" exec -T -e SMOKE_API="http://127.0.0.1:8000/api/v1" -e SMOKE_ORIGIN="http://127.0.0.1:${port}" \
  -e SMOKE_COOKIE_JAR=/tmp/smoke-cookies -e SMOKE_CSRF="$csrf_token" app sh -c \
  'cat > /tmp/container-phase4-smoke.py && python /tmp/container-phase4-smoke.py' < scripts/container-phase4-smoke.py)"
printf '%s\n' "$phase4_evidence"
# Preserve a copy of the Phase 4 data, replay migration 0007 -> 0008 only on
# that copy, and check the backfilled groups without touching the live DB.
"${compose[@]}" exec -T app python - <<'PY'
import sqlite3
with sqlite3.connect("/data/app.sqlite3") as source, sqlite3.connect("/tmp/phase4-copy.sqlite3") as target:
    source.backup(target)
PY
"${compose[@]}" exec -T app env DATABASE_URL=sqlite:////tmp/phase4-copy.sqlite3 alembic downgrade 0007_routing_destinations
"${compose[@]}" exec -T app env DATABASE_URL=sqlite:////tmp/phase4-copy.sqlite3 alembic upgrade 0008_feedback_management
"${compose[@]}" exec -T app env DATABASE_URL=sqlite:////tmp/phase4-copy.sqlite3 python -m kebivo.cli check-groups
"${compose[@]}" exec -T app python - <<'PY'
import sqlite3
with sqlite3.connect("/tmp/phase4-copy.sqlite3") as connection:
    events = connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
    occurrences = connection.execute("SELECT COALESCE(SUM(occurrence_count), 0) FROM feedback_groups").fetchone()[0]
assert events == occurrences, (events, occurrences)
print({"phase4_migration_0007_to_0008": "passed", "events": events, "occurrences": occurrences})
PY
project_body="$(curl --fail --silent --show-error -b "$cookie_jar" -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data '{"name":"Smoke project","slug":"smoke-project"}' \
  "$api/projects")"
project_id="$(printf '%s' "$project_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
printf '%s' "$project_body" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["settings"]["corrections"]["enabled"] is False'
curl --fail --silent --show-error -b "$cookie_jar" -X PUT -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data '{"tags":["internal","it"]}' "$api/projects/$project_id/default-tags" >/dev/null

token_body="$(curl --fail --silent --show-error -b "$cookie_jar" -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data '{"name":"smoke credential"}' "$api/projects/$project_id/tokens")"
token_id="$(printf '%s' "$token_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
project_token="$(printf '%s' "$token_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
printf '%s' "$token_body" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert isinstance(p["token"], str) and p["token"]'
token_list="$(curl --fail --silent --show-error -b "$cookie_jar" "$api/projects/$project_id/tokens")"
printf '%s' "$token_list" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert len(p["items"]) == 1 and "token" not in p["items"][0]'
phase5_evidence="$("${compose[@]}" exec -T -e SMOKE_API="http://127.0.0.1:8000/api/v1" \
  -e SMOKE_ORIGIN="http://127.0.0.1:${port}" -e SMOKE_UI_ORIGIN=http://127.0.0.1:8000 \
  -e SMOKE_COOKIE_JAR=/tmp/smoke-cookies -e SMOKE_CSRF="$csrf_token" \
  -e SMOKE_PROJECT_ID="$project_id" -e SMOKE_PROJECT_TOKEN="$project_token" app sh -c \
  'cat > /tmp/container-phase5-smoke.py && python /tmp/container-phase5-smoke.py' < scripts/container-phase5-smoke.py)"
printf '%s\n' "$phase5_evidence"
phase5_target_group_id="$(printf '%s' "$phase5_evidence" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().splitlines()[-1])["ac13_target_group_id"])')"
phase5_open_it_group_id="$(printf '%s' "$phase5_evidence" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().splitlines()[-1])["ac13_open_it_group_id"])')"
phase5_project_id="$(printf '%s' "$phase5_evidence" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().splitlines()[-1])["ac13_project_id"])')"
if "${compose[@]}" logs app 2>&1 | grep -Fq 'P5-FEEDBACK-PRIVATE-CANARY-71c9'; then
  echo 'feedback canary appeared in app logs' >&2
  exit 1
fi
echo '{"feedback_text_absent_from_app_logs":"passed"}'

phase6_evidence="$("${compose[@]}" exec -T -e SMOKE_API="http://127.0.0.1:8000/api/v1" \
  -e SMOKE_ORIGIN="http://127.0.0.1:${port}" -e SMOKE_COOKIE_JAR=/tmp/smoke-cookies \
  -e SMOKE_CSRF="$csrf_token" app sh -c \
  'cat > /tmp/container-phase6-smoke.py && python /tmp/container-phase6-smoke.py' < scripts/container-phase6-smoke.py)"
printf '%s\n' "$phase6_evidence"
if "${compose[@]}" logs app 2>&1 | grep -E 'P6-TIMEOUT-6b21|FAKE_REASON_CANARY-D6e8'; then
  echo 'semantic prompt or model reason canary appeared in app logs' >&2
  exit 1
fi
echo '{"semantic_prompt_and_reason_canaries_absent_from_app_logs":"passed"}'

# Built-container worker recovery; receiver control stays inside the private network.
lifecycle_destination="$(curl --fail --silent --show-error -b "$cookie_jar" -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data '{"name":"lifecycle webhook","type":"webhook","allow_private_network":true,"config":{"url":"http://webhook:8080/hook","signing":true,"auth":{"type":"none"}}}' \
  "$api/destinations")"
lifecycle_destination_id="$(printf '%s' "$lifecycle_destination" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl --fail --silent --show-error -b "$cookie_jar" -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data "{\"name\":\"lifecycle rule\",\"event_types\":[\"knowledge_gap\"],\"match_mode\":\"any\",\"tags\":[\"lifecycle\"],\"priority\":1,\"destination_ids\":[\"$lifecycle_destination_id\"],\"match_behavior\":\"stop\"}" \
  "$api/projects/$project_id/routing-rules" >/dev/null
lifecycle_submit() {
  local key="$1" question="$2"
  curl --fail --silent --show-error -X POST -H "Authorization: Bearer ${project_token}" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $key" \
    --data "{\"event_type\":\"knowledge_gap\",\"question\":\"$question\",\"tags\":[\"lifecycle\"]}" "$api/feedback" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["event_id"])'
}
reset_receiver() {
  "${compose[@]}" exec -T -e SMOKE_FAIL_FIRST="${1:-0}" -e SMOKE_HOLD_SECONDS="${2:-0}" app python -c \
    'import os,urllib.request; req=urllib.request.Request("http://webhook:8080/",data=b"",method="DELETE",headers={"X-Fail-First":os.environ["SMOKE_FAIL_FIRST"],"X-Hold-Seconds":os.environ["SMOKE_HOLD_SECONDS"]}); urllib.request.urlopen(req,timeout=3).close()' >/dev/null
}
wait_receiver_hits() {
  local expected="$1" attempt count
  for attempt in $(seq 1 80); do
    count="$("${compose[@]}" exec -T app python -c 'import json,urllib.request; print(len(json.load(urllib.request.urlopen("http://webhook:8080/",timeout=3))))')"
    [[ "$count" -ge "$expected" ]] && return 0
    sleep 0.25
  done
  echo "receiver did not record $expected requests" >&2
  return 1
}
wait_delivery_status() {
  local event="$1" expected="$2" attempt state
  for attempt in $(seq 1 100); do
    state="$(curl --fail --silent -b "$cookie_jar" "$api/deliveries?event_id=$event" 2>/dev/null | \
      python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; print(rows[0]["status"] if rows else "missing")' 2>/dev/null)" || state="unavailable"
    [[ "$state" == "$expected" ]] && return 0
    sleep 0.25
  done
  echo "event $event delivery did not reach $expected (last: $state)" >&2
  return 1
}

# Deletion cancels both completed and scheduled webhook deliveries. First prove
# a delivered event reached the receiver, then prove a failed attempt is held
# in retry_scheduled state before deleting it.
reset_receiver
delete_delivered_canary='P5-DELETE-DELIVERED-CANARY-8c17'
delete_delivered_event="$(lifecycle_submit phase5-delete-delivered "$delete_delivered_canary")"
wait_delivery_status "$delete_delivered_event" delivered
curl --fail --silent --show-error -b "$cookie_jar" -X DELETE "${write_headers[@]}" \
  "$api/admin/feedback/$delete_delivered_event" | python3 -c 'import json,sys; assert json.load(sys.stdin)["group_deleted"] is True'
delivered_calls_after_delete="$("${compose[@]}" exec -T -e SMOKE_EVENT_ID="$delete_delivered_event" app python - <<'PY'
import json, os, urllib.request
rows = json.load(urllib.request.urlopen("http://webhook:8080/", timeout=3))
assert sum(json.loads(row["body"])["event_id"] == os.environ["SMOKE_EVENT_ID"] for row in rows) == 1
print(len(rows))
PY
)"
sleep 2
"${compose[@]}" exec -T -e SMOKE_EVENT_ID="$delete_delivered_event" -e SMOKE_EXPECTED_CALLS="$delivered_calls_after_delete" app python - <<'PY'
import json, os, urllib.request
rows = json.load(urllib.request.urlopen("http://webhook:8080/", timeout=3))
calls = sum(json.loads(row["body"])["event_id"] == os.environ["SMOKE_EVENT_ID"] for row in rows)
assert calls == 1 and len(rows) == int(os.environ["SMOKE_EXPECTED_CALLS"])
PY

reset_receiver 50
delete_retry_canary='P5-DELETE-RETRY-CANARY-2de9'
delete_retry_event="$(lifecycle_submit phase5-delete-retry "$delete_retry_canary")"
"${compose[@]}" exec -T -e SMOKE_EVENT_ID="$delete_retry_event" app python - <<'PY'
import os, sqlite3, time
from datetime import datetime, timedelta, timezone
connection = sqlite3.connect("/data/app.sqlite3", timeout=5)
for _ in range(200):
    row = connection.execute("SELECT id, status FROM deliveries WHERE event_id=?", (os.environ["SMOKE_EVENT_ID"],)).fetchone()
    if row and row[1] == "retry_scheduled":
        connection.execute("UPDATE deliveries SET next_attempt_at=? WHERE id=?", ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), row[0]))
        connection.commit()
        print("retry_scheduled")
        break
    time.sleep(0.05)
else:
    raise SystemExit("delivery did not enter retry_scheduled")
connection.close()
PY
wait_delivery_status "$delete_retry_event" retry_scheduled
curl --fail --silent --show-error -b "$cookie_jar" -X DELETE "${write_headers[@]}" \
  "$api/admin/feedback/$delete_retry_event" | python3 -c 'import json,sys; assert json.load(sys.stdin)["group_deleted"] is True'
sleep 2
"${compose[@]}" exec -T -e SMOKE_EVENT_ID="$delete_retry_event" app python - <<'PY'
import json, os, urllib.request
rows = json.load(urllib.request.urlopen("http://webhook:8080/", timeout=3))
assert sum(json.loads(row["body"])["event_id"] == os.environ["SMOKE_EVENT_ID"] for row in rows) == 1
PY
"${compose[@]}" exec -T -e SMOKE_DELIVERED_ID="$delete_delivered_event" -e SMOKE_RETRY_ID="$delete_retry_event" \
  -e SMOKE_DELIVERED_CANARY="$delete_delivered_canary" -e SMOKE_RETRY_CANARY="$delete_retry_canary" app python - <<'PY'
import os, sqlite3
connection = sqlite3.connect("/data/app.sqlite3")
for event_id in (os.environ["SMOKE_DELIVERED_ID"], os.environ["SMOKE_RETRY_ID"]):
    assert connection.execute("SELECT 1 FROM feedback_events WHERE id=?", (event_id,)).fetchone() is None
    assert connection.execute("SELECT 1 FROM deliveries WHERE event_id=?", (event_id,)).fetchone() is None
    rows = connection.execute("SELECT details_json FROM audit_events WHERE action='feedback.deleted' AND subject_id=?", (event_id,)).fetchall()
    assert rows and all(os.environ["SMOKE_DELIVERED_CANARY"] not in row[0] and os.environ["SMOKE_RETRY_CANARY"] not in row[0] for row in rows)
connection.close()
PY
for delete_canary in "$delete_delivered_canary" "$delete_retry_canary"; do
  search_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -b "$cookie_jar" \
    "$api/admin/groups?project_id=$project_id&q=$delete_canary")"
  [[ "$search_status" == 200 ]]
  curl --fail --silent --show-error -b "$cookie_jar" "$api/admin/groups?project_id=$project_id&q=$delete_canary" \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["items"] == []'
done
"${compose[@]}" exec -T app python -m kebivo.cli check-groups
"${compose[@]}" exec -T app python - <<'PY'
import sqlite3
connection = sqlite3.connect("/data/app.sqlite3")
events = connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
occurrences = connection.execute("SELECT COALESCE(SUM(occurrence_count), 0) FROM feedback_groups").fetchone()[0]
assert events == occurrences, (events, occurrences)
print({"events": events, "occurrences": occurrences})
connection.close()
PY
echo '{"delivered_and_retry_scheduled_deletion":"passed","receiver_received_no_post_delete_call":true,"audit_and_search_canaries_absent":true}'

reset_receiver
"${compose[@]}" stop webhook >/dev/null
recovery_events=()
for index in 1 2 3; do
  recovery_events+=("$(lifecycle_submit "phase4-recovery-$index" "Phase4 recovery $index")")
done
"${compose[@]}" restart app >/dev/null
"${compose[@]}" start webhook >/dev/null
for recovery_event in "${recovery_events[@]}"; do wait_delivery_status "$recovery_event" delivered; done
curl --fail --silent --show-error -b "$cookie_jar" \
  "$api/admin/groups?project_id=$phase5_project_id&status=open&tag=it" \
  | python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; assert [r["id"] for r in rows] == [sys.argv[1]], rows' "$phase5_open_it_group_id"
curl --fail --silent --show-error -b "$cookie_jar" \
  "$api/admin/groups?project_id=$phase5_project_id&status=resolved&tag=it" \
  | python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; assert [r["id"] for r in rows] == [sys.argv[1]], rows' "$phase5_target_group_id"
echo '{"ac13_filters_survive_container_restart":"passed"}'

# Killing the app mid-request leaves an interrupted attempt. Startup recovers
# the lease and retries the same delivery ID (the receiver can observe both).
reset_receiver 0 5
crash_event="$(lifecycle_submit phase4-crash-recovery 'Phase4 crash recovery')"
wait_receiver_hits 1
"${compose[@]}" kill -s KILL app >/dev/null
"${compose[@]}" up -d app >/dev/null
ready=0
for attempt in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${port}/health/ready" >/dev/null; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]]
wait_receiver_hits 2
wait_delivery_status "$crash_event" delivered
crash_delivery_id="$(curl --fail --silent --show-error -b "$cookie_jar" "$api/deliveries?event_id=$crash_event" | \
  python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; assert len(rows)==1; print(rows[0]["id"])')"
"${compose[@]}" exec -T app python -c \
  'import json,urllib.request; rows=json.load(urllib.request.urlopen("http://webhook:8080/",timeout=3)); assert len(rows)==2; ids=[r["headers"]["webhook-id"] for r in rows]; assert ids[0]==ids[1]'
curl --fail --silent --show-error -b "$cookie_jar" "$api/deliveries/$crash_delivery_id" | \
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["status"]=="delivered" and any(a["result"]=="interrupted" for a in p["attempts"]), p'

# A slow request finishes within the Compose grace period and leaves no active
# delivery lease after the app starts again.
reset_receiver 0 3
graceful_event="$(lifecycle_submit phase4-graceful-stop 'Phase4 graceful stop')"
wait_receiver_hits 1
"${compose[@]}" stop app >/dev/null
"${compose[@]}" start app >/dev/null
ready=0
for attempt in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${port}/health/ready" >/dev/null; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]]
wait_delivery_status "$graceful_event" delivered
curl --fail --silent --show-error -b "$cookie_jar" "$api/deliveries?event_id=$graceful_event" | \
  python3 -c 'import json,sys; rows=json.load(sys.stdin)["items"]; assert rows and all(row["status"] != "processing" for row in rows)'

# Exercise durable Phase 2 ingestion, idempotency, authorization and inbox reads.
gap_payload='{"event_type":"knowledge_gap","question":"Smoke: how do I reset access?","context":"Acceptance smoke context","tags":["IT","Customer  Service"],"agent_id":"smoke-agent","metadata":{"source":"container-smoke"}}'
submission_headers=(-H "Authorization: Bearer ${project_token}" -H 'Content-Type: application/json' -H 'Idempotency-Key: smoke-gap-1')
submission="$(curl --fail --silent --show-error -X POST "${submission_headers[@]}" --data "$gap_payload" "$api/feedback")"
event_id="$(printf '%s' "$submission" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["status"] == "accepted" and p["processing_status"] == "pending"; print(p["event_id"])')"
replay_headers="$(mktemp)"
trap 'rm -f "$cookie_jar" "$replay_headers"; cleanup' EXIT
replay="$(curl --fail --silent --show-error -X POST "${submission_headers[@]}" --data "$gap_payload" -D "$replay_headers" "$api/feedback")"
grep -qi 'Idempotent-Replayed: true' "$replay_headers"
printf '%s' "$replay" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["event_id"] == sys.argv[1]' "$event_id"
conflict_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -X POST "${submission_headers[@]}" \
  --data '{"event_type":"knowledge_gap","question":"changed"}' "$api/feedback")"
[[ "$conflict_status" == 409 ]]
forbidden_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -X POST \
  -H "Authorization: Bearer ${project_token}" -H 'Content-Type: application/json' \
  --data '{"event_type":"knowledge_correction","question":"q","correction":"a"}' "$api/feedback")"
[[ "$forbidden_status" == 403 ]]
# MCP acceptance uses separate gap-only and correction-scoped credentials. Keep
# the gap-only token alive until the MCP matrix has checked revocation itself.
correction_token_body="$(curl --fail --silent --show-error -b "$cookie_jar" -H 'Content-Type: application/json' \
  "${write_headers[@]}" --data '{"name":"smoke correction credential","scopes":["feedback:gap","feedback:correction"]}' \
  "$api/projects/$project_id/tokens")"
correction_token="$(printf '%s' "$correction_token_body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
mcp_url="http://127.0.0.1:${port}/mcp"
mcp_container_url="http://127.0.0.1:8000/mcp"
mcp_marker='MCP-SMOKE-QUESTION-7f29'
mcp_replay_label='mcp-smoke-idempotency-7f29'

# Exercise the official Python SDK in the built app container. The policy is
# changed in SQLite between requests to verify that visibility is not cached.
mcp_results="$("${compose[@]}" exec -T -e MCP_URL="$mcp_container_url" -e MCP_PROJECT_ID="$project_id" \
  -e MCP_GAP_TOKEN="$project_token" -e MCP_CORRECTION_TOKEN="$correction_token" \
  -e MCP_MARKER="$mcp_marker" -e MCP_IDEMPOTENCY_KEY="$mcp_replay_label" app python - <<'PY'
import asyncio, json, os, sqlite3
from contextlib import asynccontextmanager
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

db = sqlite3.connect('/data/app.sqlite3')
def policy(enabled):
    row = db.execute('SELECT settings_json FROM projects WHERE id=?', (os.environ['MCP_PROJECT_ID'],)).fetchone()
    settings = json.loads(row[0]); settings.setdefault('corrections', {})['enabled'] = enabled
    db.execute('UPDATE projects SET settings_json=? WHERE id=?', (json.dumps(settings, separators=(',', ':')), os.environ['MCP_PROJECT_ID']))
    db.commit()

@asynccontextmanager
async def session(token, version):
    headers = {'Authorization': 'Bearer ' + token, 'MCP-Protocol-Version': version}
    http = __import__('httpx2').AsyncClient(headers=headers)
    async with http:
        async with streamable_http_client(os.environ['MCP_URL'], http_client=http, terminate_on_close=False) as streams:
            async with ClientSession(*streams[:2]) as client:
                if version == '2026-07-28': await client.discover()
                else: await client.initialize()
                assert client.protocol_version == version, client.protocol_version
                yield client

async def run():
    event_ids=[]
    for version in ('2026-07-28', '2025-11-25'):
        key = os.environ['MCP_IDEMPOTENCY_KEY'] + '-' + version
        policy(False)
        async with session(os.environ['MCP_CORRECTION_TOKEN'], version) as client:
            assert [t.name for t in (await client.list_tools()).tools] == ['report_knowledge_gap']
            try: await client.call_tool('submit_knowledge_correction', {'question':'hidden','correction':'hidden'})
            except MCPError as e: assert e.code == -32602 and 'Unknown tool' in str(e)
            else: raise AssertionError('hidden correction call succeeded')
            first = await client.call_tool('report_knowledge_gap', {'question':os.environ['MCP_MARKER'], 'idempotency_key':key})
            result = first.structured_content
            assert result['event_id'] and result['status'] == 'accepted', result
            if version == '2026-07-28':
                assert set(result) == {'event_id', 'status'}, result
            event_ids.append(result['event_id'])
            replay = await client.call_tool('report_knowledge_gap', {'question':os.environ['MCP_MARKER'], 'idempotency_key':key})
            assert replay.structured_content['event_id'] == result['event_id'] and replay.structured_content['replayed'] is True
            conflict = await client.call_tool('report_knowledge_gap', {'question':'changed marker','idempotency_key':key})
            assert conflict.is_error and conflict.structured_content['error']['code'] == 'idempotency_conflict'
            policy(True)
            assert {t.name for t in (await client.list_tools()).tools} == {'report_knowledge_gap','submit_knowledge_correction'}
            corrected = await client.call_tool('submit_knowledge_correction', {'question':'Correction smoke question','correction':'Correction smoke answer'})
            assert corrected.structured_content['verification_status'] == 'unverified'
            event_ids.append(corrected.structured_content['event_id'])
            policy(False)
            assert [t.name for t in (await client.list_tools()).tools] == ['report_knowledge_gap']
            try: await client.call_tool('submit_knowledge_correction', {'question':'hidden','correction':'hidden'})
            except MCPError as e: assert e.code == -32602 and 'Unknown tool' in str(e)
            else: raise AssertionError('disabled correction call succeeded')
        # A gap-only credential cannot discover or invoke the correction tool.
        policy(True)
        async with session(os.environ['MCP_GAP_TOKEN'], version) as gap_client:
            assert [t.name for t in (await gap_client.list_tools()).tools] == ['report_knowledge_gap']
            try: await gap_client.call_tool('submit_knowledge_correction', {'question':'hidden','correction':'hidden'})
            except MCPError as e: assert e.code == -32602 and 'Unknown tool' in str(e)
            else: raise AssertionError('gap-only correction call succeeded')
    policy(False)
    print(json.dumps({'protocol_versions':['2026-07-28','2025-11-25'],'event_ids':event_ids,'gap':'accepted','replay':'replayed','conflict':'idempotency_conflict','correction':'unverified','visibility':'passed'}))
asyncio.run(run())
PY
)"
mcp_event_ids="$(printf '%s' "$mcp_results" | python3 -c 'import json,sys; print(" ".join(json.loads(sys.stdin.read().splitlines()[-1])["event_ids"]))')"
mcp_event_id="${mcp_event_ids%% *}"
# Prove MCP and REST share the same idempotency key space for identical input.
cross_channel="$(curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer ${correction_token}" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${mcp_replay_label}-2026-07-28" --data "{\"event_type\":\"knowledge_gap\",\"question\":\"${mcp_marker}\"}" "$api/feedback")"
printf '%s' "$cross_channel" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["event_id"] == sys.argv[1]' "$mcp_event_id"

# Run the repository's documented Python example from inside the app image.
docs_marker='MCP-DOCS-QUESTION-8b41'
cat examples/mcp_client.py | "${compose[@]}" exec -T app sh -c 'cat > /tmp/mcp_client.py'
docs_output="$("${compose[@]}" exec -T -e PROJECT_TOKEN="$correction_token" app python /tmp/mcp_client.py \
  --url http://127.0.0.1:8000 --question "$docs_marker")"
docs_event_id="$(printf '%s' "$docs_output" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().splitlines()[-1])["event_id"])')"
mcp_event_ids="$mcp_event_ids $docs_event_id"

# Independent official TypeScript SDK client in an ephemeral scratch directory.
ts_scratch="$(mktemp -d /tmp/kebivo-mcp-ts.XXXXXX)"
(cd "$ts_scratch" && npm init -y >/dev/null && npm install --save-exact @modelcontextprotocol/sdk@1.32.1 --silent)
cat > "$ts_scratch/client.mjs" <<'JS'
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
const transport = new StreamableHTTPClientTransport(new URL(process.env.MCP_URL), {
  requestInit: { headers: { Authorization: `Bearer ${process.env.MCP_TOKEN}` } },
});
const client = new Client({ name: 'kebivo-container-acceptance', version: '1.0.0' });
await client.connect(transport);
const listed = await client.listTools();
if (!listed.tools.some((tool) => tool.name === 'report_knowledge_gap')) throw new Error('gap tool missing');
const result = await client.callTool({ name: 'report_knowledge_gap', arguments: { question: 'MCP-TS-QUESTION-9c53' } });
if (result.isError || result.structuredContent?.status !== 'accepted') throw new Error(JSON.stringify(result));
console.log(JSON.stringify({ sdk: '1.32.1', protocol: transport.protocolVersion, tools: listed.tools.map((tool) => tool.name), event_id: result.structuredContent.event_id }));
await client.close();
JS
ts_result="$(cd "$ts_scratch" && MCP_URL="$mcp_url" MCP_TOKEN="$correction_token" node client.mjs)"
ts_event_id="$(printf '%s' "$ts_result" | python3 -c 'import json,sys; p=json.loads(sys.stdin.read().splitlines()[-1]); assert p["sdk"] == "1.32.1" and p["protocol"] == "2025-11-25", p; print(p["event_id"])')"
mcp_event_ids="$mcp_event_ids $ts_event_id"
# Verify every MCP event through the detail route. The group acceptance seed
# intentionally makes the admin list exceed its first page before this check.
for mcp_id in $mcp_event_ids; do
  mcp_detail="$(curl --fail --silent --show-error -b "$cookie_jar" "$api/admin/feedback/$mcp_id")"
  printf '%s' "$mcp_detail" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["system"]["submitted_via"] == "mcp", p'
done
curl --fail --silent --show-error -b "$cookie_jar" "$api/admin/feedback/$event_id" | \
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["agent_reported"]["question"] == "Smoke: how do I reset access?", p'

# Informational latency sample requested by the acceptance plan. Use batches
# below the default per-token/IP limits, with rolling-window waits between them.
health_stop="$(mktemp)"
health_result="$(mktemp)"
rm -f "$health_stop" "$health_result"
(
  while [[ ! -e "$health_stop" ]]; do
    if curl --fail --silent "http://127.0.0.1:${port}/health/live" | grep -q '"status":"ok"'; then
      printf 'responsive\n' > "$health_result"
    fi
    sleep 0.25
  done
) &
health_pid=$!
perf_script="$(mktemp)"
cat > "$perf_script" <<'PY'
import asyncio, json, os, time
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def run():
    timings=[]
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+os.environ['MCP_TOKEN']}) as http:
        async with streamable_http_client(os.environ['MCP_URL'], http_client=http, terminate_on_close=False) as streams:
            async with ClientSession(*streams[:2]) as client:
                await client.discover()
                for batch in range(3):
                    for _ in range(100):
                        start=time.perf_counter()
                        result=await client.call_tool('report_knowledge_gap', {'question':'MCP-PERF-QUESTION-5da0'})
                        timings.append((time.perf_counter()-start)*1000)
                        assert not result.is_error and result.structured_content['status']=='accepted'
                    if batch < 2: await asyncio.sleep(61)
    ordered=sorted(timings)
    print(json.dumps({'calls':len(timings),'p95_ms':round(ordered[284],2),'median_ms':round(ordered[149],2),'protocol':'2026-07-28'}))
asyncio.run(run())
PY
perf_result="$("${compose[@]}" exec -T -e MCP_URL="$mcp_container_url" -e MCP_TOKEN="$correction_token" \
  app python - < "$perf_script")"
rm -f "$perf_script"
touch "$health_stop"
wait "$health_pid"
grep -q '^responsive$' "$health_result"
perf_summary="$(printf '%s' "$perf_result" | python3 -c 'import json,sys; p=json.loads(sys.stdin.read().splitlines()[-1]); assert p["calls"] == 300 and p["protocol"] == "2026-07-28"; print("300 calls, p95="+str(p["p95_ms"])+" ms, median="+str(p["median_ms"])+" ms")')"
mcp_perf_marker='MCP-PERF-QUESTION-5da0'

# Claude Code is available; isolate its local config and remove the temporary
# registration after verifying the documented command succeeds.
claude_scratch="$(mktemp -d /tmp/kebivo-claude-mcp.XXXXXX)"
CLAUDE_CONFIG_DIR="$claude_scratch" claude mcp add --transport http --scope local kebivo-smoke "$mcp_url" \
  --header "Authorization: Bearer ${correction_token}" >/dev/null
CLAUDE_CONFIG_DIR="$claude_scratch" claude mcp remove kebivo-smoke >/dev/null

# Confirm admin cookies do not authenticate MCP, revoked credentials return
# HTTP 401, and transport-level protections use the documented status codes.
large_body_file="$(mktemp)"
trap 'rm -f "$cookie_jar" "$replay_headers" "$large_body_file"; cleanup' EXIT
python3 -c 'import json,sys; p={"event_type":"knowledge_gap","question":""}; size=131073-len(json.dumps(p,separators=(",",":")).encode()); p["question"]="x"*size; body=json.dumps(p,separators=(",",":")); assert len(body.encode()) == 131073; sys.stdout.write(body)' > "$large_body_file"
cookie_mcp_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -b "$cookie_jar" \
  -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$mcp_url")"
[[ "$cookie_mcp_status" == 401 ]]
curl --fail --silent --show-error -b "$cookie_jar" -X POST "${write_headers[@]}" \
  "$api/projects/$project_id/tokens/$token_id/revoke" | grep -q '"revoked_at"'
revoked_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -X POST \
  -H "Authorization: Bearer ${project_token}" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' --data '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$mcp_url")"
[[ "$revoked_status" == 401 ]]
host_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Host: unlisted.invalid' \
  -H "Authorization: Bearer ${correction_token}" -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$mcp_url")"
[[ "$host_status" == 421 ]]
origin_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Origin: https://unlisted.invalid' \
  -H "Authorization: Bearer ${correction_token}" -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"ping"}' "$mcp_url")"
[[ "$origin_status" == 403 ]]
mcp_oversize_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -H "Authorization: Bearer ${correction_token}" \
  -H 'Content-Type: application/json' --data-binary "@$large_body_file" "$mcp_url")"
[[ "$mcp_oversize_status" == 413 ]]
oauth_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:${port}/.well-known/oauth-protected-resource")"
[[ "$oauth_status" == 404 ]]
large_status="$(curl --silent --output /dev/null --write-out '%{http_code}' -X POST \
  -H "Authorization: Bearer ${project_token}" -H 'Content-Type: application/json' --data-binary "@$large_body_file" "$api/feedback")"
[[ "$large_status" == 413 ]]

# Exercise the persistent SQLite volume directly, then verify submitted feedback
# survives an app restart.
"${compose[@]}" exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.sqlite3'); c.execute('CREATE TABLE IF NOT EXISTS smoke_marker (value TEXT NOT NULL)'); c.execute('DELETE FROM smoke_marker'); c.execute(\"INSERT INTO smoke_marker VALUES ('survives-recreate')\"); c.commit()"
"${compose[@]}" restart app

ready=0
for attempt in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:${port}/health/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  "${compose[@]}" logs
  echo "application did not recover after container recreation" >&2
  exit 1
fi
"${compose[@]}" exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.sqlite3'); marker=c.execute('SELECT value FROM smoke_marker').fetchone(); assert marker == ('survives-recreate',), marker; print('smoke marker survived container recreation')"
curl --fail --silent "http://127.0.0.1:${port}/" | grep -q '<div id="root">'
curl --fail --silent "http://127.0.0.1:${port}/inbox" | grep -q '<div id="root">'
detail="$(curl --fail --silent --show-error -b "$cookie_jar" "$api/admin/feedback/$event_id")"
printf '%s' "$detail" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["agent_reported"]["question"] == "Smoke: how do I reset access?", p; assert {t["slug"] for t in p["tags"]} == {"it","internal","customer-service"}, p; assert all(t["from_default"] for t in p["tags"] if t["slug"] in {"it","internal"}), p; assert all(t["from_submission"] for t in p["tags"] if t["slug"] in {"it","customer-service"}), p; assert p["system"]["processing_status"] == "completed", p; assert p["routing"]["processing_status"] == "completed", p'
echo '{"phase2_feedback_detail_after_restart":"passed"}'
"${compose[@]}" exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.sqlite3'); assert c.execute(\"SELECT count(*) FROM feedback_events WHERE id='$event_id'\").fetchone()[0] == 1, 'feedback event missing'; assert c.execute(\"SELECT count(*) FROM feedback_events WHERE project_id='$project_id'\").fetchone()[0] >= 1, 'project has no feedback'; assert c.execute(\"SELECT count(*) FROM idempotency_records WHERE project_id='$project_id' AND key='smoke-gap-1'\").fetchone()[0] == 1, 'idempotency record missing'; assert c.execute(\"SELECT count(*) FROM processing_jobs WHERE event_id='$event_id' AND status='completed'\").fetchone()[0] == 1, 'processing job not complete'"
echo '{"phase2_sqlite_rows_after_restart":"passed"}'
curl --fail --silent --show-error -b "$cookie_jar" "$api/auth/session" | grep -q 'smoke-admin@example.invalid'
curl --fail --silent --show-error -b "$cookie_jar" "$api/projects/$project_id" | grep -q 'smoke-project'
for mcp_id in $mcp_event_ids; do
  mcp_detail="$(curl --fail --silent --show-error -b "$cookie_jar" "$api/admin/feedback/$mcp_id")"
  printf '%s' "$mcp_detail" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["system"]["submitted_via"] == "mcp", p; assert p["agent_reported"]["question"] in {"MCP-SMOKE-QUESTION-7f29", "Correction smoke question", "MCP-DOCS-QUESTION-8b41", "MCP-TS-QUESTION-9c53"}, p'
done
echo '{"mcp_feedback_details_after_restart":"passed"}'
curl --fail --silent --show-error -b "$cookie_jar" -X POST "${write_headers[@]}" \
  "$api/auth/logout" | grep -q '"status":"ok"'
session_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -b "$cookie_jar" "$api/auth/session")"
if [[ "$session_status" != 401 ]]; then
  echo "logout did not revoke the browser session" >&2
  exit 1
fi
logs_file="$(mktemp)"
trap 'rm -f "$cookie_jar" "$replay_headers" "$large_body_file" "$logs_file" "$health_stop" "$health_result"; cleanup' EXIT
curl --fail --silent --show-error \
  "http://127.0.0.1:${port}/health/live?search=smoke-query-canary-812f" >/dev/null
"${compose[@]}" logs app > "$logs_file"
for marker in "$mcp_marker" "$mcp_replay_label" "$project_token" "$correction_token" "$docs_marker" \
  "MCP-TS-QUESTION-9c53" "$mcp_perf_marker" "Correction smoke question" "Correction smoke answer" \
  "Smoke: how do I reset access?" "changed marker" "smoke-url-path-canary-73d2" \
  "smoke-token-canary-48a1" "smoke-header-canary-2c19" \
  "smoke-smtp-password-canary-b419" "smoke-query-canary-812f" \
  "P5-FEEDBACK-PRIVATE-CANARY-71c9" "P5-ANSWER-PRIVATE-CANARY-c1a8" \
  "P5 admin-only note" "P5-SEARCH-DELETE-CANARY-06b2" \
  "P5-DELETE-DELIVERED-CANARY-8c17" "P5-DELETE-RETRY-CANARY-2de9"; do
  if grep -Fq "$marker" "$logs_file"; then
    echo "container logs exposed a submitted marker or credential" >&2
    exit 1
  fi
done
if grep -Fq 'whsec_' "$logs_file"; then
  echo "container logs exposed a signing secret" >&2
  exit 1
fi
echo "container smoke passed: Phase 0-2; Python MCP SDK (2026-07-28, 2025-11-25); Python docs example; TypeScript SDK 1.32.1 (2025-11-25); Claude Code configuration; correction policy; idempotency; security; inbox; restart durability; log hygiene; MCP latency $perf_summary; health remained responsive during the sample"
