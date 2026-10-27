import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, before, test } from "node:test";
import { Kebivo, KebivoConnectionError, KebivoError, KebivoHTTPError } from "./index.mjs";

let server;
let url;
let handler;
before(async () => {
  server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    handler(request, response, JSON.parse(body));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  url = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

test("gap and correction use the same API surface and token", async () => {
  const seen = [];
  handler = (request, response, body) => {
    seen.push({ headers: request.headers, body });
    response.writeHead(202, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ event_id: "event-1", status: "accepted", processing_status: "pending" }));
  };
  const client = new Kebivo(url, "secret");
  assert.equal((await client.report_gap("Where is the guide?", { tags: ["docs"] })).event_id, "event-1");
  await client.submit_correction("Where?", "In the handbook", { source_reference: "handbook" });
  assert.equal(seen[0].body.event_type, "knowledge_gap");
  assert.deepEqual(seen[0].body.tags, ["docs"]);
  assert.equal(seen[1].body.event_type, "knowledge_correction");
  assert.equal(seen[1].body.correction, "In the handbook");
  assert.equal(seen[1].body.source_reference, "handbook");
  assert.equal(seen[0].headers.authorization, "Bearer secret");
  assert.ok(seen[0].headers["idempotency-key"]);
  assert.notEqual(seen[0].headers["idempotency-key"], seen[1].headers["idempotency-key"]);
});

test("retries 429 and 5xx with one key and body", async () => {
  const seen = [];
  handler = (request, response, body) => {
    seen.push({ key: request.headers["idempotency-key"], body });
    if (seen.length < 3) {
      response.writeHead(seen.length === 1 ? 429 : 503, { "Retry-After": "0", "Content-Type": "application/json" });
      response.end('{"error":{"code":"rate_limited","message":"Wait"}}');
    } else {
      response.writeHead(202, { "Content-Type": "application/json" });
      response.end('{"event_id":"event-2","status":"accepted","processing_status":"pending"}');
    }
  };
  assert.equal((await new Kebivo(url, "secret").report_gap("Q", { idempotency_key: "fixed" })).event_id, "event-2");
  assert.equal(seen.length, 3);
  assert.deepEqual(seen.map(({ key }) => key), ["fixed", "fixed", "fixed"]);
  assert.deepEqual(seen[0].body, seen[2].body);
});

test("other 4xx fails immediately with typed details", async () => {
  let calls = 0;
  handler = (_request, response) => {
    calls++;
    response.writeHead(422, { "Content-Type": "application/json", "X-Request-ID": "req-1" });
    response.end('{"error":{"code":"validation_error","message":"Invalid request","details":{"field":"question"}}}');
  };
  await assert.rejects(new Kebivo(url, "secret").report_gap(""), (error) => {
    assert.ok(error instanceof KebivoHTTPError);
    assert.equal(error.status_code, 422);
    assert.equal(error.code, "validation_error");
    assert.equal(error.message, "Invalid request");
    assert.equal(error.request_id, "req-1");
    assert.deepEqual(error.details, { field: "question" });
    return true;
  });
  assert.equal(calls, 1);
});

test("429 waits for Retry-After", async () => {
  let calls = 0;
  handler = (_request, response) => {
    calls++;
    response.writeHead(calls === 1 ? 429 : 202, { "Retry-After": "2", "Content-Type": "application/json" });
    response.end(calls === 1 ? '{"error":{"code":"rate_limited","message":"Wait"}}' : '{"event_id":"event-3"}');
  };
  const started = Date.now();
  await new Kebivo(url, "secret").report_gap("Q");
  assert.ok(Date.now() - started >= 1900);
  assert.equal(calls, 2);
});

test("redirect is refused and does not send the token elsewhere", async () => {
  handler = (_request, response) => {
    response.writeHead(302, { Location: "https://example.com/collect" });
    response.end();
  };
  await assert.rejects(new Kebivo(url, "secret").report_gap("Q"), (error) => error instanceof KebivoHTTPError && error.status_code === 302);
});

test("bad success JSON is a Kebivo error", async () => {
  handler = (_request, response) => {
    response.writeHead(202);
    response.end("not JSON");
  };
  await assert.rejects(new Kebivo(url, "secret").report_gap("Q"), KebivoError);
});

test("connection failures exhaust into a typed error", async () => {
  const unused = await new Promise((resolve) => {
    const socket = createServer();
    socket.listen(0, "127.0.0.1", () => {
      const port = socket.address().port;
      socket.close(() => resolve(port));
    });
  });
  await assert.rejects(new Kebivo(`http://127.0.0.1:${unused}`, "secret", { timeout: 0.1 }).report_gap("Q"), KebivoConnectionError);
});
