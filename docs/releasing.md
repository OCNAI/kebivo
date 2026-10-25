# Releasing Kebivo

A release is a `vX.Y.Z` tag, a multi-platform image on GHCR and a GitHub Release. Security reports go through GitHub private vulnerability reporting (`SECURITY.md`). Kebivo is licensed under Apache-2.0 (`LICENSE`).

The sole version source is `pyproject.toml`. The current version is `0.1.0`. The container build accepts `KEBIVO_VERSION`, `KEBIVO_REVISION`, `KEBIVO_CREATED`, and `KEBIVO_SOURCE` build arguments and writes OCI title, version, revision, created, source, and license (`Apache-2.0`) labels. Compose builds `kebivo:${KEBIVO_VERSION:-local}` locally; it never pulls a registry image by default. The published image is `ghcr.io/ocnai/kebivo:<version>`, also tagged `latest`, for `linux/amd64` and `linux/arm64`. `GET /api/v1/admin/settings` and `python -m kebivo.cli version` show the version to administrators. Health responses do not include it.

From a clean checkout:

```sh
make release-check VERSION=0.1.0 PREVIOUS_REF=<previous-release-tag-or-baseline-commit>
```

The check verifies the version and clean tree, then runs `make check`, `make audit`, a labelled local image build, `make smoke`, `make e2e`, and the image-to-image upgrade check. It prints the local image ID. The upgrade check uses a temporary worktree, two local images, and a disposable Docker volume. It initializes the older image through its API, starts the current image on the same volume, checks existing table counts and content hashes, group integrity, old credentials, and a new token-authenticated submission. It removes its temporary containers, volume, and worktree on exit. Use the previous release tag. For 0.1.0, the first release, use the root commit of the public repository.

## Publishing

1. Bump the version in `pyproject.toml` and `server.json` (`version` and the image tag in `identifier`), update `CHANGELOG.md`, and merge to `main` with green CI.
2. Push an annotated tag `vX.Y.Z` on that commit. `.github/workflows/release.yml` runs `make release-check` (the upgrade check starts from the previous `v*.*.*` tag, or the root commit if there is none), then builds `linux/amd64` and `linux/arm64` with QEMU, pushes `ghcr.io/ocnai/kebivo:X.Y.Z` and `:latest` with the workflow's `GITHUB_TOKEN`, and checks the labels on both platforms. Manual dispatch of the same workflow runs the checks without publishing.
3. Create the GitHub Release from the tag, with notes taken from `CHANGELOG.md`.
4. Check that the image pulls without a login: `docker logout ghcr.io; docker pull ghcr.io/ocnai/kebivo:X.Y.Z`. A new GHCR package starts private; an organization owner makes it public once under the package's **Package settings**.
5. Run the **MCP Registry** workflow (`.github/workflows/mcp-registry.yml`, manual dispatch) to publish `server.json`. See [MCP Registry metadata](mcp-registry.md).

## Repository history

The public repository `OCNAI/kebivo` started from a single root commit made with `scripts/prepare-public-history.sh`, which builds a one-commit `public-main` branch from a clean `main` and checks it for private markers before it is pushed. Development history before that commit is not public.
