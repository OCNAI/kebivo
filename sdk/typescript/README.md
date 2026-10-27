# Kebivo TypeScript SDK

An ESM client for the [Kebivo REST feedback API](../../docs/rest-api.md). Node 20+ and global `fetch` are required. It has zero runtime dependencies and is not published to npm.

Use a project token with `feedback:gap` scope. Create one token per service, keep it on the server, send requests over HTTPS, and revoke it if it leaks. Corrections additionally need `feedback:correction` scope and an enabled corrections policy.

Install from a local checkout with `npm install ./sdk/typescript`, or copy `sdk/typescript/index.mjs` into your application and import it by relative path. The optional `index.d.mts` supplies TypeScript declarations when installing the package; copy it alongside `index.mjs` if you want types with the single-file route.

```js
import { Kebivo, KebivoError } from "@kebivo/sdk";

const client = new Kebivo(process.env.KEBIVO_URL, process.env.KEBIVO_TOKEN, { timeout: 10 });
try {
  const accepted = await client.report_gap("How do I reset my VPN?", {
    tags: ["it"], agent_id: "support-bot",
  });
  console.log(accepted.event_id);
} catch (error) {
  if (error instanceof KebivoError) console.error("Could not report feedback:", error);
  else throw error;
}
```

`submit_correction(question, correction, options)` submits an unverified correction and accepts `source_reference`. Both methods accept `tags`, `previous_answer`, `context`, `agent_id`, `conversation_id`, `source_event_id`, `user_reference`, `metadata`, and `idempotency_key`. The options use the REST field names. They return the accepted response (`event_id`, `status`, `processing_status`). Corrections need `feedback:correction` scope and an enabled corrections policy.

Each call generates one idempotency key unless supplied. The client reuses the key and body across up to four attempts, retrying 429, 5xx, and connection failures with 1, 2, and 4 second backoff. It waits at least `Retry-After`, capped at 60 seconds. Other 4xx and redirects fail immediately; redirects cannot forward the token. The timeout is in seconds per attempt and defaults to 10.

`KebivoHTTPError` exposes `status_code`, `code`, `details`, and `request_id`. `KebivoConnectionError` means no response arrived after retries. Both inherit `KebivoError`. Catch errors at your application boundary so feedback failure does not interrupt the user's answer. Keep project tokens on the server; never embed them in browser code.
