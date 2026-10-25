// Integration snippets shown on the project page. This module has no imports
// and only erasable type syntax so the acceptance suite can load it with
// `node` and execute every snippet against a running server.
export type FeedbackKind = 'knowledge_gap' | 'knowledge_correction';

export const SAMPLE_QUESTION = 'How do I reset my corporate VPN?';
export const SAMPLE_IDEMPOTENCY_KEY = 'vpn-question-001';
export const TOKEN_PLACEHOLDER = '<PROJECT_TOKEN>';

export function sampleBody(kind: FeedbackKind): Record<string, unknown> {
  return {
    event_type: kind,
    question: SAMPLE_QUESTION,
    tags: ['it'],
    ...(kind === 'knowledge_correction'
      ? { correction: 'Use the account recovery page.', source_reference: 'https://example.org/help' }
      : {}),
  };
}

export function restSnippets(origin: string, kind: FeedbackKind): Record<'JSON' | 'cURL' | 'Python' | 'TypeScript', string> {
  const endpoint = `${origin}/api/v1/feedback`;
  const body = JSON.stringify(sampleBody(kind));
  return {
    JSON: JSON.stringify(sampleBody(kind), null, 2),
    cURL: `curl -i ${endpoint} \\
  -H "Authorization: Bearer $KEBIVO_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -H 'Idempotency-Key: ${SAMPLE_IDEMPOTENCY_KEY}' \\
  --data '${body}'`,
    Python: `import json, os, urllib.request

url = os.environ.get("KEBIVO_URL", "${origin}").rstrip("/") + "/api/v1/feedback"
body = ${body}
request = urllib.request.Request(url, json.dumps(body).encode(), headers={
    "Authorization": "Bearer " + os.environ["KEBIVO_TOKEN"],
    "Content-Type": "application/json",
    "Idempotency-Key": "${SAMPLE_IDEMPOTENCY_KEY}",
})
with urllib.request.urlopen(request) as response:
    print(response.status, response.read().decode())`,
    TypeScript: `const response = await fetch("${endpoint}", {
  method: "POST",
  headers: {
    "Authorization": \`Bearer \${process.env.KEBIVO_TOKEN}\`,
    "Content-Type": "application/json",
    "Idempotency-Key": "${SAMPLE_IDEMPOTENCY_KEY}",
  },
  body: JSON.stringify(${body}),
});
console.log(response.status, await response.json());`,
  };
}

export function mcpConfig(origin: string): string {
  return JSON.stringify(
    { mcpServers: { kebivo: { type: 'http', url: `${origin}/mcp`, headers: { Authorization: `Bearer ${TOKEN_PLACEHOLDER}` } } } },
    null,
    2,
  );
}

export function claudeCommand(origin: string): string {
  return `claude mcp add --transport http kebivo ${origin}/mcp --header "Authorization: Bearer ${TOKEN_PLACEHOLDER}"`;
}
