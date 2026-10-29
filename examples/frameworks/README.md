# Framework examples

Copy-paste starting points for connecting agent frameworks to Kebivo. Each
folder has its own README and dependency list. They talk to Kebivo over the
documented MCP endpoint (`/mcp`) or REST API (`/api/v1/feedback`). They are not
an SDK and are not part of the `make e2e` acceptance suite.

| Folder | Framework | Connects over | Model in the example |
| --- | --- | --- | --- |
| [`langchain/`](langchain/) | LangChain (Python) | REST | None; local keyword matching |
| [`llamaindex/`](llamaindex/) | LlamaIndex (Python) | MCP | OpenAI or any OpenAI-compatible server |
| [`claude/`](claude/) | Claude Code, Claude Desktop | MCP | Claude |
| [`openai-agents/`](openai-agents/) | OpenAI Agents SDK (Python) | MCP | OpenAI or any OpenAI-compatible server |
| [`langgraph/`](langgraph/) | LangGraph via LangChain `create_agent` | MCP | OpenAI or any OpenAI-compatible server |
| [`vercel-ai-sdk/`](vercel-ai-sdk/) | Vercel AI SDK (TypeScript) | MCP | OpenAI or any OpenAI-compatible server |
| [`open-webui/`](open-webui/) | Open WebUI | REST tool, or MCP | Whatever Open WebUI uses |
| [`dify/`](dify/) | Dify | MCP | Whatever the Dify app uses |

Every example needs a running Kebivo and a project token (create one on the
project page in the admin UI). The scripts read `KEBIVO_URL` (default
`http://127.0.0.1:8000`) and `KEBIVO_TOKEN`. The MCP agent scripts take `--list-tools`, which checks the connection without calling a model. The LangChain and LlamaIndex scripts instead run a known and an unmatched question.

A model may not call the tool when it should. Pair the tool with the
[agent instructions](../agent-instructions.md), and use REST from your own code
where every report matters.

Each README has a **Tested** section that says what was run and what was not.
To forward reports into Slack, Linear or Jira, see the
[webhook recipes](../recipes/).
