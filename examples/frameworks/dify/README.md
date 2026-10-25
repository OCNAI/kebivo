# Dify

Dify (1.6 and later) imports tools from MCP servers over HTTP, which is what
Kebivo's `/mcp` endpoint speaks. No plugin or code is needed.

> **Untested.** This setup follows the Dify documentation as of 2026-10-21
> ([Tools → MCP](https://github.com/langgenius/dify-docs/blob/main/en/self-host/use-dify/workspace/tools.mdx)).
> A Dify stack was not run for this example. The Kebivo side (streamable HTTP
> with a static `Authorization` header) was checked with the other MCP clients
> in [`../`](../).

## Dependencies

- A Dify instance (self-hosted or Dify Cloud) that can reach Kebivo.
- A Kebivo project token with `feedback:gap`.

## Network

Dify must reach Kebivo's `/mcp` URL from its `api` container (and from Dify's
servers for Dify Cloud, which needs a public HTTPS address).

For self-hosted Dify and Kebivo on one machine, attach Kebivo to Dify's Compose
network so that Dify can use `http://kebivo:8000/mcp`:

```bash
# Dify's default network is docker_default when it runs from dify/docker
docker network connect --alias kebivo docker_default <kebivo app container, e.g. kebivo-app-1>
```

Kebivo checks the `Host` header on `/mcp`. Allow the host Dify uses, then restart
Kebivo:

```bash
MCP_ALLOWED_HOSTS=kebivo:8000
```

See [Host and reverse proxy setup](../../../docs/mcp.md#host-and-reverse-proxy-setup) in the MCP guide. Behind a public
URL that is already in `ALLOWED_ORIGINS`, no extra entry is needed.

## Add Kebivo in Dify

1. **Tools → MCP → Add MCP Server (HTTP)**.
2. **Server URL**: `http://kebivo:8000/mcp` (or your public `https://…/mcp`).
3. **Name**: `Kebivo`. **Server Identifier**: `kebivo`. Apps refer to the
   server by this identifier, so do not change it later.
4. **Advanced options → Custom headers**: `Authorization` =
   `Bearer <project token>`. Kebivo uses a static token, not OAuth.
5. Save. Dify connects and imports `report_knowledge_gap` (and
   `submit_knowledge_correction` when the token has `feedback:correction`).
6. In an Agent app or a workflow Agent node, add the Kebivo tool and paste the
   [agent instructions](../../agent-instructions.md) into the instructions.

Dify can also pass a per-caller header, for example
`{{request.headers.X-Kebivo-Token}}`, but then every Service API caller needs a
Kebivo token. A single project token in a static header is simpler.

## Troubleshooting

- **401**: wrong or revoked token, or the header is missing the `Bearer ` prefix.
- **421 or a Host error**: add the host and port Dify uses to `MCP_ALLOWED_HOSTS`.
- **Connection refused or timeout**: Kebivo binds to `127.0.0.1` by default
  (`APP_BIND_ADDRESS`), so containers outside its network cannot reach the
  published port; use the shared network above. Self-hosted Dify sends some
  outbound requests through its `ssrf_proxy` service; if that blocks the
  Kebivo address, allow it in the proxy configuration.
