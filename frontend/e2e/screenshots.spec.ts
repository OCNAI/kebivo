import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { ADMIN_EMAIL, ADMIN_PASSWORD, setLlmScenario, signIn } from './helpers';
import { seed, type Seeded } from './seed';

// README screenshots (docs/images, docs/assets/screenshots). Only runs from `make e2e-ui SCREENSHOTS=1`,
// on a fresh stack with no administrator: 00 creates the admin through the setup link with the
// built-in sample data, captures it, removes it, then seeds the database for the other screenshots.
const OUT = path.resolve(process.cwd(), '../docs/images');
const HERO = path.resolve(process.cwd(), '../docs/assets/screenshots');
test.skip(!process.env.E2E_SCREENSHOTS, 'set by scripts/e2e-ui.sh screenshots');
test.describe.configure({ mode: 'serial' });
test.setTimeout(180_000);

let data: Seeded;
let page: Page;

test.beforeAll(async ({ browser }) => {
  mkdirSync(OUT, { recursive: true });
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

test('00 first-run setup with the built-in sample data', async ({ browser, baseURL }) => {
  const setup = await (await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })).newPage();
  await setup.goto(`/setup#token=${process.env.E2E_SETUP_TOKEN}`);
  await expect(setup.getByRole('heading', { name: 'Create your administrator account' })).toBeVisible();
  await setup.getByLabel('Email address').fill(ADMIN_EMAIL);
  await setup.getByLabel('Password', { exact: true }).fill(ADMIN_PASSWORD);
  await expect(setup.getByRole('checkbox', { name: /Start with sample data/ })).toBeChecked();
  await setup.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await setup.screenshot({ path: path.join(OUT, '00-first-run.png') });
  await setup.getByRole('button', { name: 'Create account' }).click();
  await expect(setup.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  // The inbox opens on the shop sample: newest activity first.
  const top = setup.locator('.group-table tbody tr').first();
  await expect(top).toContainText('Do you ship to Canada?');
  await expect(top).toContainText('Shop assistant');
  await expect(top.locator('td').nth(5)).toHaveText('7');
  const firstInbox = path.resolve(process.cwd(), '../artifacts/first-run/sample-inbox.png');
  mkdirSync(path.dirname(firstInbox), { recursive: true });
  await setup.screenshot({ path: firstInbox, fullPage: true });

  // The README hero: the sample inbox down to the last shop row, 1600 px wide, without the one-off setup notice.
  const hero = await (await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1.25 })).newPage();
  await signIn(hero);
  await expect(hero.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await hero.goto('/inbox');
  const shopRows = hero.locator('.group-table tbody tr').filter({ hasText: 'Shop assistant' });
  await expect(shopRows.first()).toContainText('Do you ship to Canada?');
  await hero.waitForLoadState('networkidle');
  const lastShop = (await shopRows.last().boundingBox())!;
  await hero.screenshot({ path: path.join(HERO, 'inbox.png'), fullPage: true, clip: { x: 0, y: 0, width: 1280, height: Math.ceil(lastShop.y + lastShop.height + 8) } });

  await setup.getByRole('button', { name: 'Remove sample data' }).click();
  await setup.getByRole('dialog').getByRole('button', { name: 'Remove sample data' }).click();
  await expect(setup.getByText('Sample data removed.')).toBeVisible();
  await setup.context().close();
  await hero.context().close();
  data = await seed(baseURL!);
});

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

test('02 inbox default filters', async () => {
  await page.goto('/inbox');
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await expect(page.locator('.more-filters')).not.toHaveAttribute('open', '');
  await expect(page.locator('.group-table tbody tr').first()).toBeVisible();
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
