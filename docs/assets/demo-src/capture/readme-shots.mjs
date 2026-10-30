// Seeds sample shop support questions into a fresh 0.2.0 container and captures the README screenshots.
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
const project = await admin('POST', '/projects', { name: 'Shop assistant', slug: 'shop-assistant' });
const token = (await admin('POST', `/projects/${project.id}/tokens`, { name: 'Shop assistant' })).token;
const submit = async (q, tags) => {
  const r = await bare.post('/api/v1/feedback', { headers: { Authorization: `Bearer ${token}` }, data: { event_type: 'knowledge_gap', question: q, tags } });
  if (r.status() !== 202) throw new Error(await r.text());
};
const QUESTIONS = [
  ['Do you ship to Canada?', ['shipping'], 7],
  ['Can I return something after 30 days?', ['returns'], 5],
  ['Do you have a student discount?', ['discounts'], 4],
  ['Can I change my delivery address after ordering?', ['orders', 'shipping'], 3],
  ['Do you offer gift wrapping?', ['gifts'], 2],
  ['When will the blue jacket be back in stock?', ['products'], 2],
  ['Can I pay with Klarna?', ['payments'], 1],
  ['Do you ship to PO boxes?', ['shipping'], 1],
];
for (const [q, tags, n] of QUESTIONS) for (let i = 0; i < n; i++) await submit(q, tags);
await new Promise((r) => setTimeout(r, 2500));
// Triage a few so the status counts are not all "Open".
const groups = (await admin('GET', '/admin/groups?limit=50')).items;
const by = (q) => groups.find((g) => g.representative.question_excerpt === q)?.id;
await admin('PATCH', `/admin/groups/${by('Can I change my delivery address after ordering?')}`, { status: 'in_progress' });
await admin('PATCH', `/admin/groups/${by('Can I pay with Klarna?')}`, { status: 'resolved' });
const top = by('Do you ship to Canada?');
await admin('POST', `/admin/groups/${top}/notes`, { body: 'The shipping page needs a clear answer about Canada.' });

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
await page.goto('/inbox?sort=occurrences_desc');
await page.getByRole('link', { name: 'Do you ship to Canada?' }).waitFor();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${OUT}raw-inbox.png`, fullPage: true });
await page.getByRole('link', { name: 'Do you ship to Canada?' }).click();
await page.getByRole('heading', { name: 'Occurrences' }).waitFor();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${OUT}raw-group.png`, fullPage: true });
await browser.close();
console.log('done');
