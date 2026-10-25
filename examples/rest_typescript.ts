// Submit one knowledge gap to Kebivo with the fetch API in Node 24.
//
//   KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
//     node examples/rest_typescript.ts "How do I reset my corporate VPN?"
//
// Node runs this file directly (built-in type stripping, no build step). One
// Idempotency-Key is reused for every attempt; 429, 5xx and network errors are
// retried with backoff, other 4xx answers stop at once.

type Feedback = {
  event_type: "knowledge_gap";
  question: string;
  tags: string[];
};

const RETRYABLE = new Set([429, 500, 502, 503, 504]);

async function submit(baseUrl: string, token: string, body: Feedback, attempts = 4): Promise<number> {
  const key = process.env.IDEMPOTENCY_KEY ?? crypto.randomUUID();
  let delay = 1000;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    let wait = delay;
    try {
      const response = await fetch(`${baseUrl.replace(/\/$/, "")}/api/v1/feedback`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(10_000),
      });
      const text = await response.text();
      if (response.ok) {
        const replayed = response.headers.get("Idempotent-Replayed") === "true";
        console.log(JSON.stringify({ http_status: response.status, replayed, ...JSON.parse(text) }));
        return 0;
      }
      if (!RETRYABLE.has(response.status) || attempt === attempts) {
        console.log(JSON.stringify({ http_status: response.status, body: text }));
        return 1;
      }
      const retryAfter = Number(response.headers.get("Retry-After"));
      if (Number.isFinite(retryAfter) && retryAfter > 0) wait = Math.max(wait, retryAfter * 1000);
    } catch (error) {
      if (attempt === attempts) {
        console.log(JSON.stringify({ http_status: null, error: String(error) }));
        return 1;
      }
    }
    console.error(`attempt ${attempt} failed; retrying in ${Math.round(wait / 1000)}s`);
    await new Promise((resolve) => setTimeout(resolve, wait));
    delay *= 2;
  }
  return 1;
}

const token = process.env.KEBIVO_TOKEN;
if (!token) {
  console.error("Set KEBIVO_TOKEN to a project token");
  process.exit(2);
}
const question = process.argv[2] ?? "How do I reset my corporate VPN?";
process.exitCode = await submit(process.env.KEBIVO_URL ?? "http://127.0.0.1:8000", token, {
  event_type: "knowledge_gap",
  question,
  tags: ["it"],
});
