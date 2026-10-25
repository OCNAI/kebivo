# Preparing a Kebivo release candidate

Release preparation is local. A `v*` tag, a registry push and a GitHub Release require separate owner approval. Security reports go through GitHub private vulnerability reporting (`SECURITY.md`). Kebivo is licensed under Apache-2.0 (`LICENSE`).

The sole version source is `pyproject.toml`. The current candidate version is `0.1.0`. The container build accepts `KEBIVO_VERSION`, `KEBIVO_REVISION`, `KEBIVO_CREATED`, and `KEBIVO_SOURCE` build arguments and writes OCI title, version, revision, created, source, and license (`Apache-2.0`) labels. Compose builds `kebivo:${KEBIVO_VERSION:-local}` locally; it never pulls a registry image by default. `GET /api/v1/admin/settings` and `python -m kebivo.cli version` show the version to administrators. Health responses do not include it.

From a clean checkout:

```sh
make release-check VERSION=0.1.0 PREVIOUS_REF=<previous-release-tag-or-baseline-commit>
```

The check verifies the version and clean tree, then runs `make check`, `make audit`, a labelled local image build, `make smoke`, `make e2e`, and the image-to-image upgrade check. It prints the local image ID. The upgrade check uses a temporary worktree, two local images, and a disposable Docker volume. It initializes the older image through its API, starts the current image on the same volume, checks existing table counts and content hashes, group integrity, old credentials, and a new token-authenticated submission. It removes its temporary containers, volume, and worktree on exit. For the first candidate use the commit before the 0.1.0 hardening work (the maintainers record it in the release notes); afterwards use the previous release tag.

`.github/workflows/release.yml` is a private candidate workflow. Manual dispatch needs a version and previous ref. A `v*.*.*` tag would also trigger it, so do not create one before owner approval. It saves a local Docker image as a short-lived workflow artifact. The `KEBIVO_PUBLISH` branch is a failing placeholder until a registry, release assets, and publication rights are separately approved and reviewed; this workflow cannot publish in its current form.

The approved registry namespace is `ghcr.io/ocnai/kebivo`; publishing the image still needs separate owner approval. Local image tags are `kebivo:<version>` and `kebivo:local`.

## Repository history

The public repository `OCNAI/kebivo` started from a single root commit made with `scripts/prepare-public-history.sh`, which builds a one-commit `public-main` branch from a clean `main` and checks it for private markers before it is pushed. Development history before that commit is not public.
