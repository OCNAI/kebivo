#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
previous_ref="${2:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo 'Set VERSION to the pyproject.toml release version (x.y.z).' >&2
  exit 2
fi
actual="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [[ "$version" != "$actual" ]]; then
  echo "VERSION $version differs from pyproject.toml $actual" >&2
  exit 2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'Release check requires a clean working tree.' >&2
  exit 2
fi
if [[ -z "$previous_ref" ]]; then
  echo 'Set PREVIOUS_REF to the last release tag or the Phase 6 gate commit.' >&2
  exit 2
fi
git rev-parse --verify "${previous_ref}^{commit}" >/dev/null

make check
make audit
revision="$(git rev-parse HEAD)"
created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker build --build-arg "KEBIVO_VERSION=$version" \
  --build-arg "KEBIVO_REVISION=$revision" \
  --build-arg "KEBIVO_CREATED=$created" \
  -t "kebivo:$version" -t kebivo:local .
label_version="$(docker image inspect "kebivo:$version" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
label_revision="$(docker image inspect "kebivo:$version" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "$label_version" != "$version" || "$label_revision" != "$revision" ]]; then
  echo 'Built image labels do not match the release version and commit.' >&2
  exit 1
fi
label_mcp_name="$(docker image inspect "kebivo:$version" --format '{{index .Config.Labels "io.modelcontextprotocol.server.name"}}')"
server_json="$(python3 -c 'import json; d = json.load(open("server.json")); print(d["name"], d["version"])')"
if [[ "$server_json" != "$label_mcp_name $version" ]]; then
  echo "server.json ($server_json) does not match the image MCP label and release version." >&2
  exit 1
fi
make smoke
if make -n e2e >/dev/null 2>&1; then
  make e2e
else
  echo 'make e2e is not defined yet; the acceptance suite is still to be added.' >&2
fi
bash scripts/upgrade-check.sh "$previous_ref"
image_id="$(docker image inspect "kebivo:$version" --format '{{.Id}}')"
echo "Release checks passed for kebivo:$version"
echo "Image ID/digest: $image_id"
echo 'Local image only. No tag, registry push, or GitHub Release was created.'
