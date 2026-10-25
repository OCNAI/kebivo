#!/usr/bin/env bash
set -euo pipefail

uv_dir=$(mktemp -d)
trap 'rm -rf "$uv_dir"' EXIT

curl -fsSL https://astral.sh/uv/0.13.1/install.sh -o "$uv_dir/install.sh"
UV_UNMANAGED_INSTALL="$uv_dir" sh "$uv_dir/install.sh"
PATH="$uv_dir:$PATH" make docs
