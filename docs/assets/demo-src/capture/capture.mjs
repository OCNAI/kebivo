// Captures real Kebivo UI for the demo. Run against a fresh local container (see ../README.md).
// All data is synthetic: example.org addresses, no real names.
import { chromium, request } from '../../../../frontend/node_modules/@playwright/test/index.mjs';
import { mkdirSync, writeFileSync } from 'node:fs';

const BASE = process.env.BASE ?? 'http://127.0.0.1:18321';
const EMAIL = 'admin@example.org', PASSWORD = process.env.PASSWORD ?? 'DemoPassw0rd-2026';
const OUT = new URL('../shots/', import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const QUESTION = 'How do I paginate the /orders endpoint?';

const api = await request.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
const login = await api.post('/api/v1/auth/login', { data: { email: EMAIL, password: PASSWORD } });
const csrf = (await login.json()).csrf_token;
const admin = async (method, path, data) => {
  const r = await api.fetch(`/api/v1${path}`, { method, data, headers: { 'X-CSRF-Token': csrf } });
  if (!r.ok()) throw new Error(`${method} ${path}: ${r.status()} ${await r.text()}`);
  return r.status() === 204 ? undefined : r.json();
};
const bare = await request.newContext({ baseURL: BASE });
let token;
const submit = async (q, tags) => {
  const r = await bare.post('/api/v1/feedback', { headers: { Authorization: `Bearer ${token}` }, data: { event_type: 'knowledge_gap', question: q, tags } });
  if (r.status() !== 202) throw new Error(await r.text());
};

const { execSync } = await import('node:child_process');
execSync('docker cp ' + new URL('./receiver.py', import.meta.url).pathname + ' kdemo:/tmp/receiver.py && docker exec -d kdemo python /tmp/receiver.py');
const project = await admin('POST', '/projects', { name: 'API Docs Assistant', slug: 'api-docs-assistant' });
const webhook = await admin('POST', '/destinations', { name: 'API docs webhook', type: 'webhook', allow_private_network: true,
  config: { url: 'http://127.0.0.1:18400/api-docs', signing: true, auth: { type: 'none' } } });
const inbox = (await admin('GET', '/destinations')).items.find((d) => d.is_system);
await admin('POST', `/projects/${project.id}/routing-rules`, { name: 'API questions to the docs team', event_types: ['knowledge_gap'],
  match_mode: 'any', tags: ['api'], priority: 10, destination_ids: [webhook.id], match_behavior: 'continue' });
await admin('PATCH', `/projects/${project.id}`, { fallback_destination_id: inbox.id });
token = (await admin('POST', `/projects/${project.id}/tokens`, { name: 'Support agent' })).token;

// Background questions make the real inbox look lived in.
for (const [q, tags, n] of [
  ['Is there a rate limit on webhook retries?', ['webhooks'], 3],
  ['Which scopes does the refresh token need?', ['auth'], 2],
  ['How do I verify the webhook signature in Node?', ['webhooks'], 2],
]) for (let i = 0; i < n; i++) await submit(q, tags);
await new Promise((r) => setTimeout(r, 2500));

const browser = await browser_launch();
async function browser_launch() {
  return chromium.launch({ ...({ executablePath: process.env.CHROMIUM ?? '/snap/bin/chromium' }) });
}
const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: 1280, height: 720 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.goto(BASE + '/');
await page.getByLabel('Email address').fill(EMAIL);
await page.getByLabel('Password').fill(PASSWORD);
await page.getByRole('button', { name: 'Sign in' }).click();
await page.getByRole('heading', { name: 'Dashboard', level: 1 }).waitFor();
const shot = async (name) => { await page.waitForLoadState('networkidle'); await page.screenshot({ path: `${OUT}${name}.png` }); };

// Real MCP call.
const rpc = (body) => bare.post('/mcp', { headers: { Authorization: `Bearer ${token}`, Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25' }, data: body });
await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'support-agent', version: '1.0' } } });
const call = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'report_knowledge_gap', arguments: { question: QUESTION, tags: ['api', 'pagination'] } } });
const text = await call.text();
const result = JSON.parse(text.includes('data:') ? text.split('data:')[1].trim() : text);
writeFileSync(`${OUT}mcp-result.json`, JSON.stringify(result.result.structuredContent, null, 2));

for (let count = 1; count <= 2; count++) {
  if (count > 1) await submit(QUESTION, ['api', 'pagination']);
  await page.goto('/inbox');
  await page.locator('tr', { hasText: QUESTION }).first().waitFor();
  await page.waitForFunction(({ q, c }) => [...document.querySelectorAll('tr')].some((r) => r.textContent.includes(q) && r.textContent.includes(String(c))), { q: QUESTION, c: count });
  await shot(`inbox-${count}`);
}
// Routing panel on the project page.
await page.goto('/');
await page.getByRole('button', { name: /API Docs Assistant/ }).first().click();
await page.getByRole('heading', { name: 'Connect an application' }).waitFor();
const panel = page.locator('[aria-labelledby="routing-title"]');
await panel.scrollIntoViewIfNeeded();
await page.waitForLoadState('networkidle');
await panel.screenshot({ path: `${OUT}routing.png` });
// Deliveries.
await page.goto('/deliveries');
await page.getByRole('row', { name: /API docs webhook/ }).first().waitFor();
await page.waitForTimeout(4000);
await page.reload();
await page.getByRole('row', { name: /API docs webhook.*delivered/ }).first().waitFor();
await shot('deliveries');
await browser.close();
console.log('done');
