# Pydantic AI

[`agent.py`](agent.py) connects Pydantic AI's MCP client to Kebivo and
registers the tools with a Pydantic AI agent. The example has no knowledge
source, so its instructions tell the agent to report unanswered questions.

## Run

Start Kebivo and create a project token with the `feedback:gap` scope using the
[quickstart](../../../docs/quickstart.md). Then set the connection details:

```sh
export KEBIVO_URL=http://127.0.0.1:8000
export KEBIVO_TOKEN='<project token>'

# Check the MCP connection without loading or calling a model:
uv run examples/frameworks/pydantic-ai/agent.py --list-tools
# {"tools": ["report_knowledge_gap"]}

# Run the example agent (MODEL defaults to gpt-5-mini):
OPENAI_API_KEY=sk-... uv run examples/frameworks/pydantic-ai/agent.py \
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
`61fd502d32926d33a51fb96ce726aaeaf1174ad4`: `uv run agent.py --list-tools`
printed `{"tools": ["report_knowledge_gap"]}`. `uv` resolved
`pydantic-ai-slim` 2.63.0.

No model call or hosted model API key was used.
