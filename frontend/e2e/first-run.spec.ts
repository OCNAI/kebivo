import AxeBuilder from '@axe-core/playwright';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { ADMIN_EMAIL, ADMIN_PASSWORD, signIn, submitGap } from './helpers';
import { loginApi } from './seed';

// Runs first in the default suite (scripts/e2e-ui.sh leaves the server without an administrator and
// passes the one-time setup link). It creates the administrator with the sample data, follows the
// first-run path to a test event, then removes the sample data so the later specs start clean.
test.skip(!process.env.E2E_SETUP_TOKEN || Boolean(process.env.E2E_SCREENSHOTS || process.env.E2E_DEMO), 'needs a fresh server and its setup link');

const ARTIFACTS = path.resolve(process.cwd(), '../artifacts/first-run');

async function starPromptLoaded(page: Page, url: string) {
  await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/admin/star-prompt') && response.request().method() === 'GET'),
    page.goto(url),
  ]);
}

async function noSeriousAxeViolations(page: Page, name: string) {
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(violations.filter(({ impact }) => impact === 'serious' || impact === 'critical').map(({ id }) => id), name).toEqual([]);
}

async function noHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
}

test('setup with sample data leads to a connected project; only a real report shows the star prompt', async ({ browser, baseURL }) => {
  test.setTimeout(120_000);
  mkdirSync(ARTIFACTS, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const externalRequests: string[] = [];
  const origin = new URL(baseURL!).origin;
  page.on('request', request => {
    const url = request.url();
    if (/^https?:/.test(url) && new URL(url).origin !== origin) externalRequests.push(url);
  });

  await page.goto(`/setup#token=${process.env.E2E_SETUP_TOKEN}`);
  await page.getByLabel('Email address').fill(ADMIN_EMAIL);
  await page.getByLabel('Password', { exact: true }).fill(ADMIN_PASSWORD);
  await expect(page.getByRole('checkbox', { name: /Start with sample data/ })).toBeChecked();
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();

  // The notice says what to do next and links both steps.
  const notice = page.locator('.sample-announcement');
  await expect(notice).toHaveText("Sample data loaded. Open Do you ship to Canada? to see seven reports collapse into one, then Connect your own app when you're ready.");
  await expect(notice.getByRole('link', { name: 'Do you ship to Canada?' })).toHaveAttribute('href', /^\/inbox\/groups\/[^/]+$/);
  await expect(page.locator('.sample-banner')).toHaveText('Sample data.Connect your own appRemove');
  expect((await page.locator('.sample-banner').boundingBox())!.height).toBeLessThanOrEqual(48);
  await page.screenshot({ path: path.join(ARTIFACTS, 'inbox-notice-1280.png') });
  await noSeriousAxeViolations(page, 'inbox with the setup notice');
  await notice.getByRole('link', { name: 'Connect your own app' }).click();

  // A first project needs only a name; the rest waits under More options with the defaults.
  await expect(page.getByRole('heading', { name: 'Create a project' })).toBeVisible();
  await expect(page.getByLabel('Slug')).toBeHidden();
  await expect(page.getByLabel(/Description/)).toBeHidden();
  await expect(page.getByLabel('Enable correction feedback')).toBeHidden();
  await page.getByLabel('Name', { exact: true }).fill('My support bot');
  await expect(page.locator('#name-help')).toContainText('Slug: my-support-bot.');
  await page.screenshot({ path: path.join(ARTIFACTS, 'new-project-1280.png') });
  await noSeriousAxeViolations(page, 'new project form');
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page.getByRole('heading', { name: /My support bot/, level: 2 })).toBeVisible();
  await expect(page.locator('.settings-summary')).toContainText('exact');

  // After the token, the connect steps come next and the configuration is folded.
  await page.getByLabel('Token name').fill('Production');
  await page.getByRole('button', { name: 'Create token' }).click();
  const tokenField = page.getByRole('textbox', { name: 'New token' });
  await expect(tokenField).toBeFocused();
  await expect(tokenField).toBeInViewport();
  await expect(page.getByRole('heading', { name: 'Connect an application' })).toBeInViewport();
  await expect(page.getByRole('button', { name: 'Send test event' })).toBeInViewport();
  await expect(page.locator('.connect-panel .snippet-code')).toBeInViewport();
  await expect(page.getByRole('button', { name: 'Add rule' })).toBeHidden();
  await expect(page.locator('.configuration-disclosure')).not.toHaveAttribute('open');
  const token = await tokenField.inputValue();
  await page.screenshot({ path: path.join(ARTIFACTS, 'project-token-1280.png') });
  await noSeriousAxeViolations(page, 'project page after the token');

  await page.getByRole('button', { name: 'Send test event' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Test event sent.' })).toBeVisible();

  const { api, admin } = await loginApi(baseURL!);
  expect(await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).toEqual({ show: false });
  await starPromptLoaded(page, '/inbox');
  await expect(page.getByText('Kebivo test event: How do I reset my corporate VPN?').first()).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);

  // The same steps on a phone, with the sample data still loaded.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const small = await phone.newPage();
  await signIn(small);
  await expect(small.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await small.goto('/inbox');
  await expect(small.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await small.evaluate(() => window.dispatchEvent(new CustomEvent('first-run-notice', { detail: 'setup:sample-loaded' })));
  await expect(small.locator('.sample-announcement')).toContainText('seven reports collapse into one');
  await noHorizontalScroll(small);
  await small.screenshot({ path: path.join(ARTIFACTS, 'inbox-notice-390.png') });
  await small.goto('/?create=1');
  await expect(small.getByRole('heading', { name: 'Create a project' })).toBeVisible();
  await small.getByLabel('Name', { exact: true }).fill('My phone bot');
  await noHorizontalScroll(small);
  await small.screenshot({ path: path.join(ARTIFACTS, 'new-project-390.png') });
  await small.getByRole('button', { name: 'Create project' }).click();
  await expect(small.getByRole('heading', { name: /My phone bot/, level: 2 })).toBeVisible();
  await small.getByLabel('Token name').fill('Phone');
  await small.getByRole('button', { name: 'Create token' }).click();
  await expect(small.getByRole('textbox', { name: 'New token' })).toBeInViewport();
  await expect(small.getByRole('heading', { name: 'Connect an application' })).toBeInViewport();
  await noHorizontalScroll(small);
  await small.screenshot({ path: path.join(ARTIFACTS, 'project-token-390.png') });
  await phone.close();

  // A real report through the REST API is what shows the star prompt.
  await submitGap(api, token, `Where is the first run guide ${Date.now()}?`, ['first-run-acceptance']);
  await expect.poll(async () => (await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).show).toBe(true);
  await starPromptLoaded(page, '/inbox');
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toBeVisible();
  expect(externalRequests).toEqual([]);

  await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/admin/star-prompt/dismiss') && response.request().method() === 'POST'),
    page.getByRole('button', { name: 'Dismiss', exact: true }).click(),
  ]);
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);
  expect(await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).toEqual({ show: false });
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);

  // Leave no sample data behind for the later specs.
  await page.getByRole('button', { name: 'Remove sample data' }).first().click();
  await page.getByRole('dialog', { name: 'Remove sample data?' }).getByRole('button', { name: 'Remove sample data' }).click();
  await expect(page.locator('.sample-banner')).toHaveCount(0);

  await context.close();
  await api.dispose();
});
