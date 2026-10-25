# OpenAI Agents SDK

[`agent.py`](agent.py) gives an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
agent the Kebivo MCP server through `MCPServerStreamableHttp`. The agent has no
documentation, so it reports the question as a knowledge gap and tells the user.

## Dependencies

Declared inline (PEP 723) in `agent.py`, so `uv run` installs them in a
throwaway environment:

- Python 3.10+
- `openai-agents>=0.24` (tested with 0.24.0, which brings `mcp` 2.3.0)

Without uv: `pip install 'openai-agents>=0.24'` and run `python agent.py`.

## Run

```bash
export KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN='<project token>'

# Connection check, no model needed:
uv run agent.py --list-tools
# {"tools": ["report_knowledge_gap"]}

# OpenAI (MODEL defaults to gpt-5-mini):
OPENAI_API_KEY=sk-... uv run agent.py "How do I reset my corporate VPN?"

# Any OpenAI-compatible server, for example Ollama (uses Chat Completions):
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama MODEL=qwen2.5:3b \
  uv run agent.py "How do I reset my corporate VPN?"
```

The script prints the tool calls and the answer as one JSON line. The report
then appears in the Kebivo inbox.

To use it in your own agent, copy the `MCPServerStreamableHttp(...)` block and
pass the server in `mcp_servers=[...]`. Put the
[agent instructions](../../agent-instructions.md) in your agent's instructions.

## Tested

Against a local Kebivo on 2026-10-21:

- `--list-tools` listed `report_knowledge_gap`.
- With Ollama `qwen2.5:3b` (`OPENAI_BASE_URL`), the agent called
  `report_knowledge_gap` and the event was accepted.
- **Untested:** the default OpenAI path (Responses API with `OPENAI_API_KEY`),
  because this run had no paid API key.
