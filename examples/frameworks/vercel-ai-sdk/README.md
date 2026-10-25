# Vercel AI SDK

[`agent.ts`](agent.ts) connects the [AI SDK](https://ai-sdk.dev/) MCP client
(`@ai-sdk/mcp`, HTTP transport) to Kebivo and lets `generateText` call
`report_knowledge_gap`. The agent has no documentation, so it reports the
question as a knowledge gap.

The example passes `schemas` to `tools()` so the model sees only `question` and
`context`. Without it, the client uses Kebivo's full input schema, and small
models fill optional fields such as `agent_id` or `idempotency_key` with made-up
values that Kebivo rejects. Add fields to the schema if your agent should send
tags or a conversation ID.

## Dependencies

From [`package.json`](package.json) (locked in `package-lock.json`):

- Node.js 22.18 or later (runs `.ts` files directly; tested with Node.js 24.14)
- `ai` 7, `@ai-sdk/mcp` 2, `@ai-sdk/openai` 4, `@ai-sdk/openai-compatible` 3, `zod` 4
- Dev only: `typescript`, `@types/node` for `npm run typecheck`

## Run

```bash
npm install
export KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN='<project token>'

# Connection check, no model needed:
node agent.ts --list-tools
# {"tools":["report_knowledge_gap"]}

# OpenAI (MODEL defaults to gpt-5-mini):
OPENAI_API_KEY=sk-... node agent.ts "How do I reset my corporate VPN?"

# Any OpenAI-compatible server, for example Ollama:
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 MODEL=qwen2.5:3b \
  node agent.ts "How do I reset my corporate VPN?"
```

In a Next.js route handler, create the MCP client per request (or reuse one
per server process), pass its tools to `generateText` or `streamText`, and
close it when the response finishes.

## Tested

Against a local Kebivo on 2026-10-21:

- `npm run typecheck` passed; `--list-tools` listed `report_knowledge_gap`.
- With Ollama `qwen2.5:3b` through `@ai-sdk/openai-compatible`, the agent made
  one `report_knowledge_gap` call and the event was accepted. (Before `schemas`
  was added, the same model looped on rejected calls with invented fields.)
- **Untested:** the default `@ai-sdk/openai` path with OpenAI's hosted models
  (no paid API key in this run).
