#!/usr/bin/env bash
# Kebivo acceptance suite: AC-01..AC-15, the §25.6 smoke, the examples, the
# restart stages and the credential canary sweep, against the built image.
#
#   bash scripts/e2e.sh            # online run, then the offline run (default)
#   bash scripts/e2e.sh online     # one mode only
#   bash scripts/e2e.sh offline
#
# Images are built while the network is available; the offline run then uses
# an `internal: true` network (compose.e2e-offline.yaml) with a fresh volume.
set -euo pipefail

modes=("$@")
[[ ${#modes[@]} -eq 0 ]] && modes=(online offline)
export SECRET_KEY="${SECRET_KEY:-$(openssl rand -hex 32)}"
export ENCRYPTION_KEY="${ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
# A private /24 for the e2e network; the runner gets .10 and is the app's DNS.
octet="$(( (RANDOM % 200) + 20 ))"
export E2E_SUBNET="${E2E_SUBNET:-172.30.${octet}.0/24}"
export E2E_RUNNER_IP="${E2E_RUNNER_IP:-${E2E_SUBNET%.0/24}.10}"
admin_email="e2e-admin@example.invalid"
admin_password="E2E-ADMIN-PASSWORD-$(openssl rand -hex 8)"
pytest_args=(${E2E_PYTEST_ARGS:-})

docker compose -f compose.yaml -f compose.e2e.yaml build app runner

run_mode() {
  local mode="$1" project="kebivo-e2e-${1}-${RANDOM}${RANDOM}"
  local files=(-f compose.yaml -f compose.e2e.yaml)
  [[ "$mode" == offline ]] && files+=(-f compose.e2e-offline.yaml)
  local compose=(docker compose -p "$project" "${files[@]}")
  # shellcheck disable=SC2064
  trap "${compose[*]} down --volumes --remove-orphans >/dev/null 2>&1 || true" RETURN
  trap "${compose[*]} down --volumes --remove-orphans >/dev/null 2>&1 || true" EXIT

  echo "== e2e ${mode}: starting ${project} on ${E2E_SUBNET}"
  "${compose[@]}" up -d --no-build
  wait_ready() {
    for _ in $(seq 1 90); do
      if "${compose[@]}" exec -T runner python -c \
        'import urllib.request; urllib.request.urlopen("http://app:8000/health/ready", timeout=2)' \
        >/dev/null 2>&1 && "${compose[@]}" exec -T runner test -f /tmp/e2e/ready; then
        return 0
      fi
      sleep 1
    done
    "${compose[@]}" logs >&2
    echo "app or runner did not become ready" >&2
    return 1
  }
  stage() {
    echo "== e2e ${mode}: stage $1"
    "${compose[@]}" exec -T -e E2E_STAGE="$1" -e E2E_MODE="$mode" \
      -e E2E_ADMIN_EMAIL="$admin_email" -e E2E_ADMIN_PASSWORD="$admin_password" \
      runner python -m pytest e2e -p no:cacheprovider -rs "${pytest_args[@]}"
  }
  wait_ready
  printf '%s\n%s\n' "$admin_password" "$admin_password" | \
    "${compose[@]}" exec -T app python -m kebivo.cli bootstrap \
      --email "$admin_email" --password-stdin >/dev/null

  stage main
  stage before_restart
  "${compose[@]}" restart app >/dev/null
  wait_ready
  stage after_restart
  stage before_kill
  "${compose[@]}" kill -s KILL app >/dev/null
  "${compose[@]}" up -d --no-build app >/dev/null
  wait_ready
  stage after_kill

  # Retention and integrity run through the documented CLI (no API exists).
  "${compose[@]}" exec -T app python -m kebivo.cli retention --dry-run
  "${compose[@]}" exec -T app python -m kebivo.cli retention --run
  "${compose[@]}" exec -T app python -m kebivo.cli check-groups

  "${compose[@]}" logs --no-color app runner | \
    "${compose[@]}" exec -T runner sh -c 'cat > /tmp/e2e/logs.txt'
  stage sweep
  echo "== e2e ${mode}: passed"
}

for mode in "${modes[@]}"; do
  case "$mode" in
    online|offline) run_mode "$mode" ;;
    *) echo "unknown mode: $mode (use online or offline)" >&2; exit 2 ;;
  esac
done
echo "e2e passed: ${modes[*]}"
