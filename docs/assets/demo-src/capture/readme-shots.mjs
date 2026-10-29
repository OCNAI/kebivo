// Seeds sample developer questions into a fresh 0.2.0 container and captures the README screenshots.
// Sample data only: example.org addresses, invented questions. Run against a fresh container (see ../README.md).
import { chromium, request } from '../../../../frontend/node_modules/@playwright/test/index.mjs';
import { mkdirSync } from 'node:fs';

const BASE = process.env.BASE ?? 'http://127.0.0.1:18322';
const EMAIL = 'admin@example.org', PASSWORD = process.env.PASSWORD ?? 'DemoPassw0rd-2026';
const OUT = process.env.OUT ?? new URL('../shots-readme/', import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });

const api = await request.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
const csrf = (await (await api.post('/api/v1/auth/login', { data: { email: EMAIL, password: PASSWORD } })).json()).csrf_token;
const admin = async (method, path, data) => {
  const r = await api.fetch(`/api/v1${path}`, { method, data, headers: { 'X-CSRF-Token': csrf } });
  if (!r.ok()) throw new Error(`${method} ${path}: ${r.status()} ${await r.text()}`);
  return r.status() === 204 ? undefined : r.json();
};
const bare = await request.newContext({ baseURL: BASE });
const project = await admin('POST', '/projects', { name: 'API Docs Assistant', slug: 'api-docs-assistant' });
const token = (await admin('POST', `/projects/${project.id}/tokens`, { name: 'Docs assistant' })).token;
const submit = async (q, tags) => {
  const r = await bare.post('/api/v1/feedback', { headers: { Authorization: `Bearer ${token}` }, data: { event_type: 'knowledge_gap', question: q, tags } });
  if (r.status() !== 202) throw new Error(await r.text());
};
const QUESTIONS = [
  ['How do I paginate the /orders endpoint?', ['api', 'pagination'], 7],
  ['Is there a rate limit on webhook retries?', ['webhooks'], 5],
  ['Which scopes does the refresh token need?', ['auth'], 4],
  ['How do I verify the webhook signature in Node?', ['webhooks', 'node'], 3],
  ['What does error code E_IDEMPOTENCY_REPLAY mean?', ['api', 'errors'], 2],
  ['Can I filter /customers by created_at?', ['api'], 2],
  ['Is the Python SDK compatible with 3.13?', ['sdk', 'python'], 1],
  ['How do I rotate an API key without downtime?', ['auth'], 1],
];
for (const [q, tags, n] of QUESTIONS) for (let i = 0; i < n; i++) await submit(q, tags);
await new Promise((r) => setTimeout(r, 2500));
// Triage a few so the status counts are not all "Open".
const groups = (await admin('GET', '/admin/groups?limit=50')).items;
const by = (q) => groups.find((g) => g.representative.question_excerpt === q)?.id;
await admin('PATCH', `/admin/groups/${by('How do I verify the webhook signature in Node?')}`, { status: 'in_progress' });
await admin('PATCH', `/admin/groups/${by('Is the Python SDK compatible with 3.13?')}`, { status: 'resolved' });
const top = by('How do I paginate the /orders endpoint?');
await admin('POST', `/admin/groups/${top}/notes`, { body: 'Cursor pagination is undocumented. Adding a section to the Orders reference.' });

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? '/snap/bin/chromium' });
const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.goto('/');
await page.getByLabel('Email address').fill(EMAIL);
await page.getByLabel('Password').fill(PASSWORD);
await page.getByRole('button', { name: 'Sign in' }).click();
await page.getByRole('heading', { name: 'Dashboard', level: 1 }).waitFor();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${OUT}raw-dashboard.png`, fullPage: true });
await page.goto('/inbox');
await page.getByRole('link', { name: 'How do I paginate the /orders endpoint?' }).waitFor();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${OUT}raw-inbox.png`, fullPage: true });
await page.getByRole('link', { name: 'How do I paginate the /orders endpoint?' }).click();
await page.getByRole('heading', { name: 'Occurrences' }).waitFor();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${OUT}raw-group.png`, fullPage: true });
await browser.close();
console.log('done');
