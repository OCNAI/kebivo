# LlamaIndex

[`agent.py`](agent.py) loads Kebivo's MCP tools with LlamaIndex's
[`llama-index-tools-mcp`](https://developers.llamaindex.ai/python/framework/module_guides/mcp/llamaindex_mcp/)
and makes them available to an `AgentWorkflow`. The example has no knowledge
source, so its instructions tell the agent to report unanswered questions.

## Run

Start Kebivo and create a project token with the `feedback:gap` scope using the
[quickstart](../../../docs/quickstart.md). Then set the connection details:

```sh
export KEBIVO_URL=http://127.0.0.1:8000
export KEBIVO_TOKEN='<project token>'

# Check the MCP connection without loading or calling a model:
uv run examples/frameworks/llamaindex/agent.py --list-tools
# {"tools": ["report_knowledge_gap"]}

# Run the example agent (MODEL defaults to gpt-5-mini):
OPENAI_API_KEY=sk-... uv run examples/frameworks/llamaindex/agent.py \
  'How do I reset my corporate VPN?'
```

`KEBIVO_URL` defaults to `http://127.0.0.1:8000`. For an OpenAI-compatible
provider, set `OPENAI_BASE_URL` and `MODEL` as well. The script connects to
`{KEBIVO_URL}/mcp` over Streamable HTTP and sends the token in
`Authorization: Bearer`.

Dependencies are declared inline using PEP 723; `uv run` installs them in an
isolated environment. Python 3.10 or newer is required.

## Tested

Against a local Compose build from Kebivo commit
`8ba5e63a1c3b3f8806d97011bf75ab4b2d36c001`: `uv run agent.py --list-tools`
printed `{"tools": ["report_knowledge_gap"]}`. `uv` resolved
`llama-index-tools-mcp` 0.6.0, `llama-index-llms-openai` 0.8.3 and
`llama-index-core` 0.14.25.

No model call or hosted model API key was used.
