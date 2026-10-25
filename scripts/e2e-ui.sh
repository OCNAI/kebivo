#!/usr/bin/env bash
# Kebivo browser end-to-end tests (Playwright, Chromium) against the built image.
#
#   bash scripts/e2e-ui.sh              # run the browser tests
#   bash scripts/e2e-ui.sh screenshots  # also regenerate docs/images/*.png
#   bash scripts/e2e-ui.sh demo         # capture frames for the README demo GIF (artifacts/demo-frames)
#
# Starts the e2e stack (compose.yaml + compose.e2e.yaml + compose.e2e-ui.yaml),
# bootstraps an admin, runs `npx playwright test` on the host against the
# published loopback ports, then removes the stack and its volume.
set -euo pipefail

cd "$(dirname "$0")/.."
export SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"
export ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
octet="$(( (RANDOM % 200) + 20 ))"
export E2E_SUBNET="${E2E_SUBNET:-172.30.${octet}.0/24}"
export E2E_RUNNER_IP="${E2E_RUNNER_IP:-${E2E_SUBNET%.0/24}.10}"
export E2E_UI_PORT="${E2E_UI_PORT:-$(( (RANDOM % 2000) + 20000 ))}"
export E2E_UI_RECEIVER_PORT="${E2E_UI_RECEIVER_PORT:-$(( E2E_UI_PORT + 2000 ))}"
export E2E_UI_LLM_PORT="${E2E_UI_LLM_PORT:-$(( E2E_UI_PORT + 4000 ))}"
admin_email="e2e-admin@example.invalid"
[[ "${1:-}" == screenshots || "${1:-}" == demo ]] && admin_email="admin@example.org"
admin_password="E2E-ADMIN-PASSWORD-$(openssl rand -hex 8)"
project="kebivo-e2e-ui-${RANDOM}${RANDOM}"
compose=(docker compose -p "$project" -f compose.yaml -f compose.e2e.yaml -f compose.e2e-ui.yaml)

docker compose -f compose.yaml -f compose.e2e.yaml build app runner
trap '"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true' EXIT

echo "== e2e-ui: starting ${project} on http://127.0.0.1:${E2E_UI_PORT}"
"${compose[@]}" up -d --no-build
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${E2E_UI_PORT}/health/ready" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:${E2E_UI_RECEIVER_PORT}/" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:${E2E_UI_LLM_PORT}/health" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
if [[ -z "${ready:-}" ]]; then
  "${compose[@]}" logs >&2
  echo "app or test doubles did not become ready" >&2
  exit 1
fi
printf '%s\n%s\n' "$admin_password" "$admin_password" | \
  "${compose[@]}" exec -T app python -m kebivo.cli bootstrap \
    --email "$admin_email" --password-stdin >/dev/null

cd frontend
export E2E_BASE_URL="http://127.0.0.1:${E2E_UI_PORT}"
export E2E_RECEIVER_URL="http://127.0.0.1:${E2E_UI_RECEIVER_PORT}"
export E2E_LLM_URL="http://127.0.0.1:${E2E_UI_LLM_PORT}"
export E2E_ADMIN_EMAIL="$admin_email" E2E_ADMIN_PASSWORD="$admin_password"
if [[ "${1:-}" == screenshots ]]; then
  export E2E_SCREENSHOTS=1
  npx playwright test screenshots.spec.ts
elif [[ "${1:-}" == demo ]]; then
  export E2E_DEMO=1
  npx playwright test demo.spec.ts
else
  npx playwright test
fi
echo "e2e-ui passed"
