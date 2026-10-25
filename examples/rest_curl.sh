#!/usr/bin/env bash
# Submit one knowledge gap to Kebivo with curl.
#
#   KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
#     bash examples/rest_curl.sh "How do I reset my corporate VPN?"
#
# One Idempotency-Key is reused for every retry. curl retries 429, 5xx and
# connection errors with backoff; other 4xx answers fail at once.
set -euo pipefail

: "${KEBIVO_TOKEN:?Set KEBIVO_TOKEN to a project token}"
url="${KEBIVO_URL:-http://127.0.0.1:8000}"
question="${1:-How do I reset my corporate VPN?}"
key="${IDEMPOTENCY_KEY:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
body="$(QUESTION="$question" python3 -c 'import json, os; print(json.dumps({"event_type": "knowledge_gap", "question": os.environ["QUESTION"], "tags": ["it"]}))')"

# Write to a file: curl truncates it before each retry, so only the final
# answer is printed.
out="$(mktemp)"
trap 'rm -f "$out"' EXIT
status=0
curl --silent --show-error --fail-with-body --output "$out" \
  --retry 4 --retry-connrefused --retry-max-time 60 \
  -X POST "${url%/}/api/v1/feedback" \
  -H "Authorization: Bearer ${KEBIVO_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${key}" \
  --data "$body" || status=$?
cat "$out"
echo
exit "$status"
