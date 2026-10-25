# MCP Registry metadata

`server.json` describes Kebivo for the [official MCP Registry](https://registry.modelcontextprotocol.io). Nothing has been published. Publishing needs owner approval (launch plan §7, item 11).

## How Kebivo is listed

Kebivo is self-hosted, so it is listed as an **OCI package** (`ghcr.io/ocnai/kebivo`) whose transport is Streamable HTTP at `http://127.0.0.1:8000/mcp` on the container the user runs. It is not listed under `remotes`:

- Registry rules say a remote server **must** be publicly reachable at its URL. Kebivo has no public instance.
- `mcp-publisher` 1.8.1 rejects a remote URL that is `http`, on loopback, or entirely a template (`{kebivo_url}/mcp`).
- `https://{kebivo_host}/mcp` with a host variable passes the validator, but it goes against the public-URL rule. Do not publish it unless the registry maintainers confirm it is acceptable.

The registry verifies that we own the image through the `io.modelcontextprotocol.server.name` label in the `Dockerfile`. That label must equal `name` in `server.json`. `kebivo/tests/test_mcp_registry_metadata.py` checks this, and `scripts/release-check.sh` checks the label on the built image.

MCP clients can start the container from this entry, but Kebivo is not usable until an administrator has been bootstrapped, a project has been created and a project token has been issued (see the README quickstart). The token goes in the `Authorization` header variable.

## Before publishing

1. OA-3 is complete: `server.json` and the `Dockerfile` label use `io.github.ocnai/kebivo`, and the image identifier uses the lower-case `ghcr.io/ocnai/kebivo` namespace.
2. After OA-5, make sure `ghcr.io/ocnai/kebivo:<version>` is public and has the label. `version` and the identifier tag must equal the `pyproject.toml` version.
3. Validate: `mcp-publisher validate server.json`. This needs no login, but it sends the file to the registry's validation endpoint.
4. With approval only: `mcp-publisher login github`, then `mcp-publisher publish`.
