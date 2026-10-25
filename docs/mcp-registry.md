# MCP Registry metadata

`server.json` describes Kebivo for the [official MCP Registry](https://registry.modelcontextprotocol.io).

## How Kebivo is listed

Kebivo is self-hosted, so it is listed as an **OCI package** (`ghcr.io/ocnai/kebivo`) whose transport is Streamable HTTP at `http://127.0.0.1:8000/mcp` on the container the user runs. It is not listed under `remotes`:

- Registry rules say a remote server **must** be publicly reachable at its URL. Kebivo has no public instance.
- `mcp-publisher` 1.8.1 rejects a remote URL that is `http`, on loopback, or entirely a template (`{kebivo_url}/mcp`).
- `https://{kebivo_host}/mcp` with a host variable passes the validator, but it goes against the public-URL rule. Do not publish it unless the registry maintainers confirm it is acceptable.

The registry verifies that we own the image through the `io.modelcontextprotocol.server.name` label in the `Dockerfile`. That label must equal `name` in `server.json`. `kebivo/tests/test_mcp_registry_metadata.py` checks this, and `scripts/release-check.sh` checks the label on the built image.

MCP clients can start the container from this entry, but Kebivo is not usable until an administrator has been bootstrapped, a project has been created and a project token has been issued (see the README quickstart). The token goes in the `Authorization` header variable.

## Publishing

The **MCP Registry** workflow (`.github/workflows/mcp-registry.yml`) publishes `server.json`. Run it by hand after a release:

1. `ghcr.io/ocnai/kebivo:<version>` must be public and carry the label. The registry pulls it anonymously and rejects private images.
2. `version` and the image tag in `identifier` must equal the `pyproject.toml` version; the workflow checks this.
3. The workflow signs in with GitHub OIDC (`mcp-publisher login github-oidc`), which proves ownership of the `io.github.OCNAI` namespace (the registry keeps the GitHub account's letter case) without a stored secret, then runs `mcp-publisher publish`.

To check the file before a release, `mcp-publisher validate server.json` needs no login, but it sends the file to the registry's validation endpoint.
