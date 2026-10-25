import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { expect, request as playwrightRequest, test, type Page } from '@playwright/test';
import { loginApi } from './seed';
import { INTERNAL_RECEIVER, resetReceiver, signIn, submitGap } from './helpers';

// Frames for the README demo GIF (docs/assets/demo.gif). Only runs from `bash scripts/e2e-ui.sh demo`
// on a fresh stack. Writes PNGs plus frames.json (file + hold in ms) to artifacts/demo-frames;
// scripts/build-demo-gif.py turns them into the GIF. All data is synthetic.
const OUT = path.resolve(process.cwd(), '../artifacts/demo-frames');
test.skip(!process.env.E2E_DEMO, 'set by scripts/e2e-ui.sh demo');
test.setTimeout(180_000);

const GAP_QUESTION = 'Where is the VPN setup guide?';
const EARLIER_QUESTION = 'How do I set up the VPN?';
const TAGLINE = "When your AI doesn't know, Kebivo makes sure someone does.";
const frames: { file: string; ms: number }[] = [];
let n = 0;

const STYLE = `
  #demo-caption { position: fixed; left: 0; right: 0; bottom: 0; z-index: 99999; padding: 14px 28px; background: #1f4a3c;
    color: #fff; font: 600 21px/1.2 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  #demo-caption b { color: #bfe3d3; margin-right: 10px; }
  .demo-ring { outline: 4px solid #1f4a3c !important; outline-offset: 3px; border-radius: 6px; }
  * { caret-color: transparent; }
`;

async function frame(page: Page, caption: string, ms: number) {
  await page.waitForLoadState('networkidle');
  await page.evaluate(({ css, text }) => {
    // The app's CSP blocks <style> elements; constructable stylesheets are allowed.
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    document.adoptedStyleSheets = [sheet];
    let bar = document.getElementById('demo-caption');
    if (!bar) { bar = document.createElement('div'); bar.id = 'demo-caption'; document.body.append(bar); }
    const [step, ...rest] = text.split('|');
    bar.innerHTML = '';
    const b = document.createElement('b'); b.textContent = step; bar.append(b, rest.join('|'));
  }, { css: STYLE, text: caption });
  const file = `${String(++n).padStart(2, '0')}.png`;
  await page.screenshot({ path: path.join(OUT, file) });
  frames.push({ file, ms });
}

function terminal(lines: { cls: string; text: string }[]) {
  const body = lines.map((l) => `<div class="${l.cls}">${l.text.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</div>`).join('');
  return `<!doctype html><meta charset="utf-8"><style>
    body { margin: 0; height: 540px; background: #eef3f0; font-family: ui-sans-serif, system-ui, sans-serif; }
    .card { position: absolute; left: 80px; right: 80px; top: 70px; background: #14201b; border-radius: 14px; padding: 28px 32px;
      box-shadow: 0 18px 40px rgba(20,32,27,.25); font: 500 22px/1.55 ui-monospace, Menlo, Consolas, monospace; color: #e7f1ec; }
    .bar { color: #8fb3a3; font-size: 17px; margin-bottom: 14px; letter-spacing: .04em; }
    .user { color: #ffffff; } .agent { color: #b8d8c9; } .call { color: #f5d98a; } .ok { color: #7fe0b0; font-weight: 700; }
    .gap { height: 10px; }
  </style><div class="card"><div class="bar">AI AGENT, CONNECTED TO KEBIVO OVER MCP</div>${body}</div>`;
}

test('record demo frames', async ({ browser, baseURL }) => {
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });
  const { admin } = await loginApi(baseURL!);
  const bare = await playwrightRequest.newContext({ baseURL: baseURL! });
  await resetReceiver(bare, 0);

  // Seed: one project, a webhook for tag `it`, some older questions (one of them the duplicate to merge).
  const project = await admin.call<{ id: string }>('POST', '/projects', { name: 'Support Assistant', slug: 'support-assistant' });
  await admin.call('PUT', `/projects/${project.id}/default-tags`, { tags: ['it', 'hr', 'billing'] });
  const webhook = await admin.call<{ id: string }>('POST', '/destinations', {
    name: 'IT webhook', type: 'webhook', allow_private_network: true,
    config: { url: INTERNAL_RECEIVER + '/it', signing: true, auth: { type: 'none' } },
  });
  const destinations = await admin.call<{ items: { id: string; is_system: boolean }[] }>('GET', '/destinations');
  const inbox = destinations.items.find((item) => item.is_system)!;
  await admin.call('POST', `/projects/${project.id}/routing-rules`, {
    name: 'IT questions to the webhook', event_types: ['knowledge_gap'], match_mode: 'any', tags: ['it'], priority: 10,
    destination_ids: [webhook.id], match_behavior: 'continue',
  });
  await admin.call('PATCH', `/projects/${project.id}`, { fallback_destination_id: inbox.id });
  const created = await admin.call<{ token: string }>('POST', `/projects/${project.id}/tokens`, { name: 'Production integration' });
  const token = created.token;
  for (const [q, tags, times] of [
    ['Why was I charged twice this month?', ['billing'], 3],
    ['Where can I find the expense policy?', ['hr'], 2],
    ['How do I connect to the office printer?', ['it', 'printer'], 2],
  ] as [string, string[], number][]) {
    for (let i = 0; i < times; i++) await submitGap(bare, token, i % 2 ? q.replace(/\?$/, '') : q, tags);
  }
  await submitGap(bare, token, EARLIER_QUESTION, ['it', 'vpn']);
  await expect.poll(async () => (await admin.call<{ items: unknown[] }>('GET', '/admin/groups?limit=100')).items.length).toBe(4);

  const page = await (await browser.newContext({ viewport: { width: 960, height: 540 } })).newPage();

  // Scene 1: a real MCP call to /mcp (stateless Streamable HTTP, JSON responses).
  const rpc = async (body: unknown) => bare.post('/mcp', {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25' },
    data: body,
  });
  const init = await rpc({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'support-agent', version: '1.0' } } });
  expect(init.ok(), await init.text()).toBe(true);
  const call = await rpc({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'report_knowledge_gap', arguments: { question: GAP_QUESTION, tags: ['it', 'vpn'] } } });
  expect(call.ok(), await call.text()).toBe(true);
  const text = await call.text();
  const result = JSON.parse(text.includes('data:') ? text.split('data:')[1].trim() : text) as { result: { structuredContent: { status: string } } };
  expect(result.result.structuredContent.status).toBe('accepted');

  const lines = [
    { cls: 'user', text: `User:  ${GAP_QUESTION}` },
    { cls: 'agent', text: 'Agent: I could not find that in my sources.' },
    { cls: 'gap', text: '' },
  ];
  const callLine = { cls: 'call', text: 'tools/call report_knowledge_gap' };
  const argsLine = { cls: 'call', text: '  tags: it, vpn' };
  await page.setContent(terminal(lines));
  await frame(page, '1|An agent reports a gap', 1400);
  await page.setContent(terminal([...lines, callLine, argsLine]));
  await frame(page, '1|An agent reports a gap', 1500);
  await page.setContent(terminal([...lines, callLine, argsLine, { cls: 'ok', text: 'status: accepted' }]));
  await frame(page, '1|An agent reports a gap', 1800);

  // Scene 2: the report lands in the inbox.
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await page.goto('/inbox');
  const row = page.locator('tr', { hasText: GAP_QUESTION }).first();
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByRole('link', { name: GAP_QUESTION }).evaluate((el) => el.classList.add('demo-ring'));
  await page.locator('.table-wrap').first().evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 80));
  await frame(page, '2|It lands in the inbox', 3500);

  // Scene 3: merge the earlier duplicate into the new group.
  await row.getByRole('link', { name: GAP_QUESTION }).click();
  await expect(page.getByRole('heading', { name: 'Occurrences' })).toBeVisible();
  await page.getByRole('button', { name: 'Merge groups' }).click();
  await page.getByLabel('Search compatible groups').fill('VPN');
  const source = page.getByLabel(new RegExp(EARLIER_QUESTION.replace('?', '\\?')));
  await source.check();
  await frame(page, '3|Duplicates merge', 2800);
  await page.getByRole('button', { name: 'Confirm merge' }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByText('2 occurrences').first()).toBeVisible();
  await frame(page, '3|Duplicates merge', 2000);
  await page.getByRole('heading', { name: 'Group history' }).evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 90));
  await expect(page.getByText('Merged').first()).toBeVisible();
  await frame(page, '3|Duplicates merge', 2600);

  // Scene 4: the webhook fired.
  await page.goto('/deliveries');
  await expect.poll(async () => (await page.getByRole('row', { name: /IT webhook/ }).count()), { timeout: 30_000 }).toBeGreaterThan(0);
  await expect(page.getByRole('row', { name: /IT webhook.*(delivered|succeeded)/i }).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole('row', { name: /IT webhook/ }).first().getByText('delivered').evaluate((el) => el.classList.add('demo-ring'));
  await frame(page, '4|The webhook fires', 4000);

  // End card, on a fresh page: the app's CSP would otherwise carry over to setContent.
  const end = await (await browser.newContext({ viewport: { width: 960, height: 540 } })).newPage();
  await end.setContent(`<!doctype html><meta charset="utf-8"><style>
    body { margin: 0; height: 540px; background: #f6f8f6; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 28px;
      font-family: ui-sans-serif, system-ui, sans-serif; color: #263d35; }
    .brand { display: flex; align-items: center; gap: 18px; font-size: 64px; font-weight: 700; letter-spacing: -.04em; }
    .brand svg { width: 72px; height: 72px; } p { margin: 0; font-size: 28px; max-width: 700px; text-align: center; line-height: 1.3; color: #35504a; }
  </style><div class="brand"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#35735e"/><g fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 8v16"/><path d="M11.5 16.5 21 8.5"/><path d="M15.5 13.1 21.5 24"/></g></svg>Kebivo</div><p>${TAGLINE.replace("'", '&#39;')}</p>`);
  const file = `${String(++n).padStart(2, '0')}.png`;
  await end.screenshot({ path: path.join(OUT, file) });
  frames.push({ file, ms: 2000 });

  writeFileSync(path.join(OUT, 'frames.json'), JSON.stringify(frames, null, 2));
  await bare.dispose();
});
