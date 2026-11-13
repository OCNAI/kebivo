#!/usr/bin/env bash
# Check what a newcomer sees on first run of a candidate image.
set -euo pipefail

image="${1:?usage: first-run-check.sh IMAGE}"
suffix="first-run-check-$$"
cleanup() {
  docker rm -f "$suffix" >/dev/null 2>&1 || true
  docker volume rm -f "$suffix" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Run the README command as written, on a fresh volume. Only the container
# name, host port, volume and image change, so it can't collide with a
# running instance.
readme_line="$(grep -m1 '^docker run ' README.md)"
read -ra args <<<"$readme_line"
command=()
for arg in "${args[@]}"; do
  case "$arg" in
    kebivo) arg="$suffix" ;;
    127.0.0.1:*:8000) arg=127.0.0.1:0:8000 ;;
    *:/data) arg="$suffix:/data" ;;
    ghcr.io/ocnai/kebivo:*) arg="$image" ;;
  esac
  command+=("$arg")
done
if [[ "${command[*]}" != "docker run --name $suffix -p 127.0.0.1:0:8000 -v $suffix:/data $image" ]]; then
  echo "README docker run line changed shape; update this check: $readme_line" >&2
  exit 1
fi
"${command[@]}" >/dev/null 2>&1 &
for _ in $(seq 60); do
  if docker logs "$suffix" 2>&1 | grep -q '^Kebivo setup: http.*/setup#token='; then
    break
  fi
  sleep 1
done
if ! docker logs "$suffix" 2>&1 | grep -q '^Kebivo setup: http.*/setup#token='; then
  echo 'README docker run did not print a Kebivo setup: URL within 60 seconds.' >&2
  docker logs "$suffix" 2>&1 | tail -20 >&2
  exit 1
fi
cleanup

# A bad setting prints one line, never a traceback.
status=0
output="$(docker run --rm -e SECRET_KEY=bad -e ENCRYPTION_KEY=bad "$image" 2>&1)" || status=$?
if [[ "$status" -eq 0 || "$(printf '%s\n' "$output" | wc -l)" -ne 1 || "$output" != SettingsError:*SECRET_KEY* ]]; then
  echo "SECRET_KEY=bad should print one SettingsError line; got (exit $status):" >&2
  printf '%s\n' "$output" >&2
  exit 1
fi
echo "First-run checks passed for $image"
