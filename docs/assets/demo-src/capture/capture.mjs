// Captures real Kebivo UI for the demo. Run against a fresh local container (see ../README.md).
// All data is synthetic: example.org addresses, no real names.
import { chromium, request } from '../../../../frontend/node_modules/@playwright/test/index.mjs';
import { mkdirSync, writeFileSync } from 'node:fs';

const BASE = process.env.BASE ?? 'http://127.0.0.1:18321';
const EMAIL = 'admin@example.org', PASSWORD = process.env.PASSWORD ?? 'DemoPassw0rd-2026';
const OUT = new URL('../shots/', import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const QUESTION = 'Where is the VPN setup guide?';

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
const project = await admin('POST', '/projects', { name: 'Support Assistant', slug: 'support-assistant' });
const webhook = await admin('POST', '/destinations', { name: 'IT docs webhook', type: 'webhook', allow_private_network: true,
  config: { url: 'http://127.0.0.1:18400/it-docs', signing: true, auth: { type: 'none' } } });
const inbox = (await admin('GET', '/destinations')).items.find((d) => d.is_system);
await admin('POST', `/projects/${project.id}/routing-rules`, { name: 'IT questions to the docs team', event_types: ['knowledge_gap'],
  match_mode: 'any', tags: ['it'], priority: 10, destination_ids: [webhook.id], match_behavior: 'continue' });
await admin('PATCH', `/projects/${project.id}`, { fallback_destination_id: inbox.id });
token = (await admin('POST', `/projects/${project.id}/tokens`, { name: 'Support agent' })).token;

// Background questions so the inbox looks lived in (billing and hr route to the inbox fallback).
for (const [q, tags, n] of [
  ['Why was I charged twice this month?', ['billing'], 3],
  ['Where can I find the expense policy?', ['hr'], 2],
  ['How do I connect to the office printer?', ['hr'], 2],
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
const call = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'report_knowledge_gap', arguments: { question: QUESTION, tags: ['it', 'vpn'] } } });
const text = await call.text();
const result = JSON.parse(text.includes('data:') ? text.split('data:')[1].trim() : text);
writeFileSync(`${OUT}mcp-result.json`, JSON.stringify(result.result.structuredContent, null, 2));

for (let count = 1; count <= 4; count++) {
  if (count > 1) await submit(QUESTION, ['it', 'vpn']);
  await page.goto('/inbox');
  await page.locator('tr', { hasText: QUESTION }).first().waitFor();
  await page.waitForFunction(({ q, c }) => [...document.querySelectorAll('tr')].some((r) => r.textContent.includes(q) && r.textContent.includes(String(c))), { q: QUESTION, c: count });
  await shot(`inbox-${count}`);
}
// Routing panel on the project page.
await page.goto('/');
await page.getByRole('button', { name: /Support Assistant/ }).first().click();
await page.getByRole('heading', { name: 'Connect an application' }).waitFor();
const panel = page.locator('[aria-labelledby="routing-title"]');
await panel.scrollIntoViewIfNeeded();
await page.waitForLoadState('networkidle');
await panel.screenshot({ path: `${OUT}routing.png` });
// Deliveries.
await page.goto('/deliveries');
await page.getByRole('row', { name: /IT docs webhook/ }).first().waitFor();
await page.waitForTimeout(4000);
await page.reload();
await page.getByRole('row', { name: /IT docs webhook.*delivered/ }).first().waitFor();
await shot('deliveries');
// Group detail: note, then resolved.
await page.goto('/inbox');
await page.locator('tr', { hasText: QUESTION }).first().getByRole('link', { name: QUESTION }).click();
await page.getByRole('heading', { name: 'Occurrences' }).waitFor();
await shot('group-open');
const gid = page.url().split('/').pop();
await admin('POST', `/admin/groups/${gid}/notes`, { body: 'Added the VPN setup guide to the IT handbook.' });
await admin('PATCH', `/admin/groups/${gid}`, { status: 'resolved' });
await page.reload();
await page.getByRole('heading', { name: 'Occurrences' }).waitFor();
await shot('group-resolved');
await page.locator('section', { has: page.getByRole('heading', { name: 'Triage' }) }).last().screenshot({ path: `${OUT}triage-resolved.png` });
await browser.close();
console.log('done');
