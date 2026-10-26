# Claude Code and Claude Desktop

Connect Claude to Kebivo's MCP endpoint so it can report questions it could not
answer. Kebivo exposes `report_knowledge_gap` to tokens with `feedback:gap`, and
`submit_knowledge_correction` to tokens that also have `feedback:correction`.

You need a running Kebivo (default `http://127.0.0.1:8000`) and a project token
from the project page in the admin UI.

## Claude Code

Claude Code speaks streamable HTTP directly. Add the server for yourself:

```bash
export KEBIVO_TOKEN='<project token>'
claude mcp add --transport http kebivo http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $KEBIVO_TOKEN"
claude mcp list   # kebivo: http://127.0.0.1:8000/mcp (HTTP) - ✔ Connected
```

Or share it with a repository: copy [`claude_code.mcp.json`](claude_code.mcp.json)
to `.mcp.json` in the repository root. It reads `KEBIVO_TOKEN` from each
user's environment, so the token is not committed. Claude Code asks each user
to approve a project server the first time it starts.

A wrong token shows `✘ Failed to connect — Server rejected the configured
Authorization header (HTTP 401)` in `claude mcp list`.

## Claude Desktop

Claude Desktop starts local MCP servers as commands, so
[`claude_desktop_config.json`](claude_desktop_config.json) runs the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge, which needs
Node.js (tested with Node.js 24). Merge its `mcpServers` entry into the file opened by **Settings →
Developer → Edit Config** (`claude_desktop_config.json`), replace
`<PROJECT_TOKEN>`, and restart Claude Desktop. The token sits in an `env`
value because some clients split `args` on spaces.

Claude Desktop's **Connectors** setting connects from Anthropic's servers, not
from your computer, so it can reach Kebivo only at a public HTTPS address.
Use the bridge for a Kebivo on your machine or private network.

Then add the [agent instructions](../../agent-instructions.md) to a project's
instructions so Claude knows when to report.

## Cursor

Cursor uses the same Kebivo HTTP endpoint and bearer header, with its own
`${env:KEBIVO_TOKEN}` interpolation syntax. Copy [`cursor_mcp.json`](cursor_mcp.json)
to `.cursor/mcp.json` in your project or merge its entry into your existing file.
Set `KEBIVO_TOKEN` in Cursor's environment. Cursor should list
`report_knowledge_gap` under available MCP tools. See the
[Cursor MCP docs](https://cursor.com/docs/mcp). The Cursor UI was not run for this example.

## Dependencies

- Claude Code: nothing besides Claude Code.
- Claude Desktop: Node.js (tested with 24) for `npx mcp-remote@0.15.1` (pinned; update deliberately).

## Tested

Against a local Kebivo on 2026-10-21:

- Claude Code 2.1.301: `claude mcp add --transport http` with a header, then
  `claude mcp list` showed `✔ Connected`; a wrong token showed the 401 above.
- `claude_desktop_config.json`: ran the configured `npx mcp-remote@0.15.1`
  command, sent MCP `initialize`, `tools/list` and a `tools/call` of
  `report_knowledge_gap` over stdio, and the event was accepted.
- On 2026-10-26, the same bridge command was run against `ghcr.io/ocnai/kebivo:0.1.1`; `initialize`, `tools/list`, and `report_knowledge_gap` returned an accepted event.
- **Untested:** the Claude Desktop and Cursor apps themselves and a model-initiated tool call in either app (no desktop session or paid model use in this run).
