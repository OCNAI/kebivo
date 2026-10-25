import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { setLlmScenario, signIn } from './helpers';
import { seed, type Seeded } from './seed';

// README screenshots (docs/images). Only runs from `make e2e-ui SCREENSHOTS=1`, on a fresh stack:
// the seed assumes an empty database and the journey spec is skipped in that mode.
const OUT = path.resolve(process.cwd(), '../docs/images');
test.skip(!process.env.E2E_SCREENSHOTS, 'set by scripts/e2e-ui.sh screenshots');
test.describe.configure({ mode: 'serial' });
test.setTimeout(180_000);

let data: Seeded;
let page: Page;

test.beforeAll(async ({ browser, baseURL }) => {
  mkdirSync(OUT, { recursive: true });
  data = await seed(baseURL!);
  page = await (await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })).newPage();
});

async function shot(name: string, target?: Locator, fullPage = false) {
  await page.waitForLoadState('networkidle');
  const file = path.join(OUT, name);
  if (target) await target.screenshot({ path: file });
  else await page.screenshot({ path: file, fullPage });
}

async function openProject(name: string) {
  await page.goto('/');
  await page.getByRole('button', { name: new RegExp(name) }).first().click();
  await expect(page.getByRole('heading', { name: 'Connect an application' })).toBeVisible();
}

test('10 sign-in', async ({ browser }) => {
  const anon = await (await browser.newContext({ viewport: { width: 640, height: 520 } })).newPage();
  await anon.goto('/');
  await expect(anon.getByRole('heading', { name: 'Sign in to your workspace' })).toBeVisible();
  await anon.screenshot({ path: path.join(OUT, '10-sign-in.png') });
});

test('01 dashboard', async () => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Top recurring open gaps' })).toBeVisible();
  await shot('01-dashboard.png');
});

test('02 inbox filtered by tag', async () => {
  await page.goto('/inbox');
  await page.getByText('More filters').click();
  await page.getByLabel('Filter by existing tag').selectOption({ label: 'it' });
  await expect(page.getByLabel('Active filters')).toBeVisible();
  await shot('02-inbox.png');
});

test('03 group detail', async () => {
  await page.goto(`/inbox/groups/${data.hrGroupId}`);
  await expect(page.getByRole('heading', { name: 'Occurrences' })).toBeVisible();
  await shot('03-group-detail.png', undefined, true);
});

test('04 connect panel', async () => {
  await openProject('Support Assistant');
  await page.getByRole('tab', { name: 'cURL' }).click();
  await shot('04-connect.png', page.locator('.connect-panel'));
});

test('05 routing panel', async () => {
  await openProject('Support Assistant');
  await page.getByLabel('Tags (comma separated)').fill('it');
  await page.getByLabel('Include project default tags').uncheck();
  await page.getByRole('button', { name: 'Simulate' }).click();
  await expect(page.getByText(/match/i).last()).toBeVisible();
  await shot('05-routing.png', page.locator('[aria-labelledby="routing-title"]'));
});

test('06 destinations', async () => {
  await page.goto('/destinations');
  await expect(page.getByText('IT webhook').first()).toBeVisible();
  await shot('06-destinations.png');
});

test('07 deliveries with an opened failed delivery', async () => {
  await page.goto('/deliveries');
  await page.getByLabel('Filter by state').selectOption('failed');
  await page.getByRole('row', { name: /failed/ }).first().getByRole('button', { name: 'Details' }).click();
  await expect(page.getByRole('heading', { name: 'Delivery details' })).toBeVisible();
  await shot('07-deliveries.png');
});

test('08 settings', async () => {
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Data retention' })).toBeVisible();
  await shot('08-settings.png');
});

test('09 LLM settings', async ({ request }) => {
  await setLlmScenario(request, 'valid');
  await page.goto('/settings/llm');
  await expect(page.getByRole('heading', { name: 'Local model' })).toBeVisible();
  await page.getByRole('button', { name: 'Test connection' }).first().click();
  await expect(page.getByText('Connection successful; expected answer received.')).toBeVisible();
  await shot('09-llm-settings.png');
});
