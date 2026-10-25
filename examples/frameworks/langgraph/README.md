# LangGraph

[`agent.py`](agent.py) loads Kebivo's MCP tools with
[`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters)
and runs them in a LangGraph agent built by LangChain's `create_agent` (the
successor of `langgraph.prebuilt.create_react_agent`). The agent has no
documentation, so it reports the question as a knowledge gap.

## Dependencies

Declared inline (PEP 723) in `agent.py`, so `uv run` installs them in a
throwaway environment:

- Python 3.10+
- `langchain>=1.4` (brings `langgraph`), `langchain-mcp-adapters>=0.3`,
  `langchain-openai>=1.7`; tested with langchain 1.4.6, langgraph 1.2.16,
  langchain-mcp-adapters 0.3.2 and langchain-openai 1.7.2.

Swap `langchain-openai` and `ChatOpenAI` for another chat model package if you
use a different provider.

## Run

```bash
export KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN='<project token>'

# Connection check, no model needed:
uv run agent.py --list-tools
# {"tools": ["report_knowledge_gap"]}

# OpenAI (MODEL defaults to gpt-5-mini):
OPENAI_API_KEY=sk-... uv run agent.py "How do I reset my corporate VPN?"

# Any OpenAI-compatible server, for example Ollama:
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama MODEL=qwen2.5:3b \
  uv run agent.py "How do I reset my corporate VPN?"
```

To use it in an existing graph, add the tools from `client.get_tools()` to your
tool node; each tool call opens a short MCP session with the configured header.

## Tested

Against a local Kebivo on 2026-10-21:

- `--list-tools` listed `report_knowledge_gap`.
- With Ollama `qwen2.5:3b`, the agent called `report_knowledge_gap` and the
  event was accepted.
- **Untested:** OpenAI's hosted models (no paid API key in this run).
