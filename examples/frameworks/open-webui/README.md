# Open WebUI

Two ways to let Open WebUI models report knowledge gaps to Kebivo.

## Option A: workspace tool over REST ([`kebivo_tool.py`](kebivo_tool.py))

A Python tool with one method, `report_knowledge_gap(question, context)`, which
posts to Kebivo's REST API (`POST /api/v1/feedback`) with an idempotency key.
It needs only the standard library and `pydantic`, which Open WebUI already has.

1. **Workspace → Tools → +** (or **Import**), paste `kebivo_tool.py`, save.
2. Open the tool's **Valves** and set:
   - `kebivo_url`: Kebivo's origin as seen from the Open WebUI server, for
     example `http://kebivo:8000` on a shared Compose network;
   - `project_token`: a Kebivo project token with `feedback:gap`;
   - `tags`: tags added to every report (default `open-webui`).
3. Enable the tool for a model (**Workspace → Models → edit → Tools**) or per
   chat, and add the [agent instructions](../../agent-instructions.md) to the
   model's system prompt.

The tool answers the model with a short sentence: the event ID on success, or
`not recorded` with the HTTP status or `unreachable`. The token is stored in
the tool's valves, which anyone who can edit the tool can read.

REST does not check the `Host` header, so no `MCP_ALLOWED_HOSTS` entry is needed.

## Option B: MCP

Open WebUI versions with native MCP support can connect to Kebivo directly:
in **Admin Settings**, add a tool server connection (the section is called
**External Tools** or **Tool Servers** depending on the version) of type
**MCP (Streamable HTTP)**, URL `http://kebivo:8000/mcp`, **Bearer** auth with
the project token. Kebivo's own tool descriptions then guide the model.
Kebivo checks the `Host` header on `/mcp`, so set
`MCP_ALLOWED_HOSTS=kebivo:8000` on Kebivo for that URL (see the
[MCP guide](../../../docs/mcp.md#host-and-reverse-proxy-setup)).

## Dependencies

- Open WebUI (tested with 0.12.3) that can reach Kebivo.
- A Kebivo project token with `feedback:gap`.

## Tested

On 2026-10-21, against a local Kebivo:

- The tool class called directly (Python 3.12, pydantic): with no token it
  asks for one; with a token the gap was accepted and appeared in the inbox
  with the `open-webui` tag; a wrong token returned `HTTP 401`; an unreachable
  URL returned `unreachable`.
- Open WebUI 0.12.3 (`ghcr.io/open-webui/open-webui:main`, Ollama
  `qwen2.5:3b`): `POST /api/v1/tools/create` imported the file with its
  frontmatter and valves, the valves were updated, and a chat completion with
  `tool_ids` made the model call `report_knowledge_gap` with the user's question.
- Option B: Open WebUI's tool server check
  (`POST /api/v1/configs/tool_servers/verify`, type `mcp`, bearer auth)
  connected to Kebivo's `/mcp` and listed `report_knowledge_gap`.
- **Untested:** the complete round trip in the chat UI, where Open WebUI runs
  the tool and answers, for either option. The CPU-only model did not finish
  within five minutes in a headless browser.
