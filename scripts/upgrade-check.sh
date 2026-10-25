#!/usr/bin/env bash
set -euo pipefail

from_ref="${1:-}"
if [[ -z "$from_ref" ]]; then
  echo 'Usage: scripts/upgrade-check.sh <from-git-ref>' >&2
  exit 2
fi
git rev-parse --verify "${from_ref}^{commit}" >/dev/null
scratch="$(mktemp -d /tmp/kebivo-upgrade.XXXXXX)"
name="kebivo-upgrade-$$"
volume="${name}-data"
old_image="kebivo-upgrade-old:$$"
new_image="kebivo-upgrade-new:$$"
old_container="${name}-old"
new_container="${name}-new"
cleanup() {
  docker rm -f "$old_container" "$new_container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  git worktree remove --force "$scratch/old" >/dev/null 2>&1 || true
  rm -rf "$scratch"
}
trap cleanup EXIT

git worktree add --detach "$scratch/old" "$from_ref" >/dev/null
old_cli=backend.cli
if [[ -f "$scratch/old/kebivo/cli.py" ]]; then old_cli=kebivo.cli; fi
docker build -q -t "$old_image" "$scratch/old" > "$scratch/old-image-id"
docker build -q --build-arg "KEBIVO_VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')" \
  --build-arg "KEBIVO_REVISION=$(git rev-parse HEAD)" -t "$new_image" . > "$scratch/new-image-id"
docker volume create "$volume" >/dev/null
password="$(openssl rand -hex 24)"
secret_key="$(openssl rand -hex 32)"
encryption_key="$(openssl rand -hex 32)"
run_app() {
  local container="$1" image="$2" worker_enabled="${3:-false}"
  docker run -d --name "$container" -p 127.0.0.1::8000 -v "$volume:/data" \
    -e SECRET_KEY="$secret_key" -e ENCRYPTION_KEY="$encryption_key" \
    -e ALLOWED_ORIGINS=http://127.0.0.1:18099 \
    -e WORKER_ENABLED="$worker_enabled" -e DELIVERY_RETRY_SCHEDULE_SECONDS=30,30,30,30,30 \
    -e FEEDBACK_RETENTION_DAYS=0 \
    -e DELIVERY_LOG_RETENTION_DAYS=0 -e AUDIT_RETENTION_DAYS=0 \
    "$image" >/dev/null
  port="$(docker port "$container" 8000/tcp | sed 's/.*://')"
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/health/ready" >/dev/null 2>&1; then
      export UPGRADE_URL="http://127.0.0.1:$port"; return
    fi
    sleep 2
  done
  docker logs "$container" >&2
  echo "Upgrade container $container did not become ready" >&2
  exit 1
}
export UPGRADE_PASSWORD="$password" UPGRADE_STATE="$scratch/state.json"
run_app "$old_container" "$old_image"
printf '%s\n%s\n' "$password" "$password" | docker exec -i "$old_container" \
  python -m "$old_cli" bootstrap --email upgrade-admin@example.invalid --password-stdin
python3 scripts/upgrade-fixture.py seed
docker stop "$old_container" >/dev/null
docker rm "$old_container" >/dev/null
run_app "$old_container" "$old_image" true
retry_info=""
for _ in $(seq 1 60); do
  retry_info="$(docker exec "$old_container" python -c 'import sqlite3; c=sqlite3.connect("/data/app.sqlite3"); r=c.execute("SELECT id,attempt_count FROM deliveries WHERE status=? ORDER BY created_at LIMIT 1", ("retry_scheduled",)).fetchone(); print(" ".join(map(str,r)) if r else "")')"
  if [[ -n "$retry_info" ]]; then break; fi
  sleep 2
done
if [[ -z "$retry_info" ]]; then
  docker logs "$old_container" >&2
  echo 'Old image did not create a retry-scheduled delivery.' >&2
  exit 1
fi
read -r retry_delivery_id retry_attempts <<< "$retry_info"
echo "Old image prepared retry-scheduled delivery with $retry_attempts attempt(s)."
docker stop "$old_container" >/dev/null
docker run --rm -i -v "$volume:/data" --entrypoint python "$old_image" - \
  < scripts/upgrade-snapshot.py > "$scratch/before.json"
run_app "$new_container" "$new_image"
head="$(docker exec "$new_container" python -c 'import sqlite3; c=sqlite3.connect("/data/app.sqlite3"); print(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])')"
expected_head="$(uv run alembic heads | awk 'NR==1 {print $1}')"
if [[ "$head" != "$expected_head" ]]; then
  echo "Expected migration $expected_head before serving; found $head" >&2
  exit 1
fi
docker exec "$new_container" python -m kebivo.cli check-groups
docker run --rm -i -v "$volume:/data" --entrypoint python "$new_image" - \
  < scripts/upgrade-snapshot.py > "$scratch/after.json"
python3 - "$scratch/before.json" "$scratch/after.json" <<'PY'
import json, sys
before, after = [json.load(open(path)) for path in sys.argv[1:]]
for table, expected in before.items():
    assert after.get(table) == expected, (table, expected, after.get(table))
print(json.dumps({"preserved_tables": len(before), "row_counts_and_checksums": "passed"}))
PY
python3 scripts/upgrade-fixture.py verify
docker stop "$new_container" >/dev/null
docker rm "$new_container" >/dev/null
run_app "$new_container" "$new_image" true
resumed=0
for _ in $(seq 1 45); do
  attempts="$(docker exec "$new_container" python -c 'import sqlite3,sys; c=sqlite3.connect("/data/app.sqlite3"); print(c.execute("SELECT attempt_count FROM deliveries WHERE id=?", (sys.argv[1],)).fetchone()[0])' "$retry_delivery_id")"
  if (( attempts > retry_attempts )); then resumed=1; break; fi
  sleep 2
done
if [[ "$resumed" != 1 ]]; then
  echo 'Old delivery was not retried by the upgraded image.' >&2
  exit 1
fi
echo "Upgrade retry resumed: $retry_attempts -> $attempts attempts."
echo 'Upgrade check passed.'
