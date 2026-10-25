// A Vercel AI SDK agent that reports knowledge gaps to Kebivo over MCP.
//
// Usage (Node.js 22.18 or newer runs TypeScript directly):
//   npm install
//   KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
//   OPENAI_API_KEY=sk-... node agent.ts "How do I reset my corporate VPN?"
//
//   # Any OpenAI-compatible server, for example Ollama:
//   OPENAI_BASE_URL=http://127.0.0.1:11434/v1 MODEL=qwen2.5:3b \
//     KEBIVO_TOKEN=<project token> node agent.ts "How do I reset my corporate VPN?"
//
//   # Check the connection without a model:
//   KEBIVO_TOKEN=<project token> node agent.ts --list-tools

import { createMCPClient } from '@ai-sdk/mcp';
import { openai } from '@ai-sdk/openai';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText, isStepCount } from 'ai';
import { z } from 'zod';

const INSTRUCTIONS = `You answer questions about company IT and HR policies.
You have no documentation available in this example, so you cannot answer
reliably. When you cannot answer, call report_knowledge_gap once with the
user's question (without personal data) and a short context, then tell the
user that you could not find a reliable answer and that the gap was reported.`;

function model() {
  const name = process.env.MODEL ?? 'gpt-5-mini';
  const baseURL = process.env.OPENAI_BASE_URL;
  if (!baseURL) return openai(name);
  const provider = createOpenAICompatible({
    name: 'openai-compatible',
    baseURL,
    apiKey: process.env.OPENAI_API_KEY,
  });
  return provider(name);
}

const token = process.env.KEBIVO_TOKEN;
if (!token) {
  console.error('Set KEBIVO_TOKEN to a Kebivo project token');
  process.exit(2);
}
const question = process.argv[2];
if (!question) {
  console.error('Give a question or --list-tools');
  process.exit(2);
}

const kebivo = await createMCPClient({
  transport: {
    type: 'http',
    url: `${(process.env.KEBIVO_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')}/mcp`,
    headers: { Authorization: `Bearer ${token}` },
  },
});

try {
  // Expose only the fields a model should fill. Without `schemas`, the client
  // uses Kebivo's full input schema, and small models tend to invent values
  // for optional fields such as agent_id or idempotency_key.
  const tools = await kebivo.tools({
    schemas: {
      report_knowledge_gap: {
        inputSchema: z.object({
          question: z.string().min(1).describe("The user's question, without personal data"),
          context: z
            .string()
            .optional()
            .describe('What you looked for and why it was not enough'),
        }),
      },
    },
  });
  if (question === '--list-tools') {
    console.log(JSON.stringify({ tools: Object.keys(tools) }));
  } else {
    const result = await generateText({
      model: model(),
      system: INSTRUCTIONS,
      prompt: question,
      tools,
      stopWhen: isStepCount(4),
    });
    const calls = result.steps.flatMap(step => step.toolCalls.map(call => call.toolName));
    console.log(JSON.stringify({ tool_calls: calls, answer: result.text }));
  }
} finally {
  await kebivo.close();
}
