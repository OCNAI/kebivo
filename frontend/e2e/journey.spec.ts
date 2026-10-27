import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import {
  ADMIN_PASSWORD, INTERNAL_LLM, INTERNAL_RECEIVER, collectCsp, receiverHits, resetReceiver,
  setLlmScenario, signIn, submitGap, watchProblems, type PageProblems,
} from './helpers';

// One serial journey: each test is a PRD §25.5 flow and builds on the one before it.
test.skip(Boolean(process.env.E2E_SCREENSHOTS || process.env.E2E_DEMO), 'screenshots and demo modes run their own specs only');
test.describe.configure({ mode: 'serial' });

const suffix = Math.random().toString(36).slice(2, 8);
const projectName = `Support bot ${suffix}`;
const destinationName = `Ops webhook ${suffix}`;
const hookPath = `/hook-${suffix}`;
const question = `How do I reset my VPN ${suffix}?`;
const billingQuestion = `Why was I charged twice ${suffix}?`;
const failingQuestion = `Where is the failing runbook ${suffix}?`;
let context: BrowserContext;
let page: Page;
let problems: PageProblems;
let token = '';

test.beforeAll(async ({ browser, request }) => {
  context = await browser.newContext();
  page = await context.newPage();
  problems = await watchProblems(page);
  await resetReceiver(request);
  await setLlmScenario(request, 'valid');
});
test.afterAll(async () => {
  await context.close();
});
test.afterEach(async () => {
  await collectCsp(page, problems);
});

test('admin login rejects a wrong password and accepts the right one', async () => {
  await signIn(page, 'not-the-password');
  await expect(page.getByRole('alert')).toBeVisible();
  // The 401 for the wrong password is the point of this step, not a defect.
  problems.consoleErrors.length = 0;
  await expect(page.getByRole('heading', { name: 'Sign in to your workspace' })).toBeVisible();
  await page.getByLabel('Password').fill(ADMIN_PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
});

test('create a project', async () => {
  await page.getByRole('button', { name: 'New project' }).click();
  await page.getByLabel('Name', { exact: true }).fill(projectName);
  await page.getByLabel('Slug').fill(`support-${suffix}`);
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Project created.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: projectName, level: 2 })).toBeVisible();
});

test('create a webhook destination', async () => {
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Destinations' }).click();
  await page.getByRole('button', { name: 'New webhook' }).click();
  await page.getByLabel('Name', { exact: true }).fill(destinationName);
  await page.getByLabel('URL').fill(INTERNAL_RECEIVER + hookPath);
  await page.getByLabel('Sign webhook requests').check();
  await page.getByLabel('Allow private network').click();
  await page.getByRole('dialog', { name: 'Allow private network access?' }).getByRole('button', { name: 'Allow private network' }).click();
  await expect(page.getByLabel('Allow private network')).toBeChecked();
  await page.getByRole('button', { name: 'Create destination' }).click();
  await expect(page.getByRole('row', { name: new RegExp(destinationName) })).toBeVisible();
});

test('create a routing rule for the "it" tag', async () => {
  await page.goto('/');
  await page.getByRole('button', { name: new RegExp(projectName) }).click();
  await page.getByRole('button', { name: 'Add rule' }).click();
  await page.getByLabel('Name', { exact: true }).last().fill(`IT to webhook ${suffix}`);
  await page.getByLabel('Tags (comma separated)').first().fill('it');
  await page.getByLabel('Destinations (1–10)').selectOption({ label: destinationName });
  await page.getByRole('button', { name: 'Save rule' }).click();
  await expect(page.getByText(`IT to webhook ${suffix}`).first()).toBeVisible();
});

test('generate a project token, shown once', async () => {
  await page.getByLabel('Token name').fill(`bot ${suffix}`);
  await page.getByRole('button', { name: 'Create token' }).click();
  const field = page.getByRole('textbox', { name: 'New token' });
  token = await field.inputValue();
  expect(token).toMatch(/^kebivo_pt_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]+$/);
  await page.getByRole('button', { name: 'I have saved it' }).click();
  await expect(field).toHaveCount(0);
  await expect(page.getByText(token)).toHaveCount(0);
});

test('submit test feedback and see it in the inbox', async ({ request }) => {
  await submitGap(request, token, question, ['it', 'vpn']);
  await submitGap(request, token, question.toUpperCase(), ['it']);
  await submitGap(request, token, billingQuestion, ['billing']);
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Inbox' }).click();
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await expect(page.getByText(question).first()).toBeVisible();
});

test('the event reaches the webhook destination', async ({ request }) => {
  await expect.poll(async () => (await receiverHits(request, hookPath)).length, { timeout: 30_000 }).toBeGreaterThan(0);
  const [hit] = await receiverHits(request, hookPath);
  expect(hit.body).toContain(question);
});

test('filter the inbox by tag', async () => {
  await page.goto('/inbox');
  await page.getByText('More filters').click();
  await page.getByLabel('Filter by existing tag').selectOption({ label: 'vpn' });
  await expect(page.getByRole('group', { name: 'Active filters' }).or(page.getByLabel('Active filters'))).toBeVisible();
  await expect(page.getByText(question).first()).toBeVisible();
  await expect(page.getByText(billingQuestion)).toHaveCount(0);
  await page.getByRole('button', { name: 'Clear filters' }).click();
  await expect(page.getByText(billingQuestion).first()).toBeVisible();
});

test('open a group and update its status; the filter survives a reload', async () => {
  await page.goto('/inbox');
  await page.getByText(question).first().click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText(question);
  await expect(page.getByRole('heading', { name: 'Occurrences' })).toBeVisible();
  await expect(page.locator('[aria-labelledby="occurrences-title"] article time').first()).toHaveAttribute('datetime', /^\d{4}-\d{2}-\d{2}T/);
  await expect(page.getByText('Time unavailable')).toHaveCount(0);
  await page.getByLabel('Group status').selectOption({ label: 'Resolved' });
  await expect(page.getByLabel('Group status')).toHaveValue('resolved');
  await page.reload();
  await expect(page.getByLabel('Group status')).toHaveValue('resolved');

  await page.goto('/inbox');
  await page.getByRole('button', { name: /^Open \d/ }).click();
  await expect(page.getByText(billingQuestion).first()).toBeVisible();
  await expect(page.getByText(question)).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(question)).toHaveCount(0);
  await page.goto('/inbox');
  await page.getByRole('button', { name: /^Resolved \d/ }).click();
  await expect(page.getByText(question).first()).toBeVisible();
});

test('a permanently failing webhook shows as failed, and a manual retry delivers it', async ({ request }) => {
  await resetReceiver(request, 100);
  await submitGap(request, token, failingQuestion, ['it']);
  await page.goto('/deliveries');
  await page.getByLabel('Filter by destination').selectOption({ label: destinationName });
  const row = page.getByRole('row', { name: new RegExp(`${destinationName}.*failed`) });
  // The log does not refresh itself; re-apply the state filter until the retries are used up.
  await expect(async () => {
    await page.getByLabel('Filter by state').selectOption('');
    await page.getByLabel('Filter by state').selectOption('failed');
    await expect(row).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 45_000 });
  await row.getByRole('button', { name: 'Details' }).click();
  await expect(page.getByRole('heading', { name: 'Delivery details' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /Attempt 1/ })).toBeVisible();

  await resetReceiver(request, 0);
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByText('Retry finished: delivered.')).toBeVisible();
  await expect.poll(async () => (await receiverHits(request, hookPath)).length).toBeGreaterThan(0);
});

test('configure an LLM provider against the fake server and test the connection', async () => {
  await page.goto('/settings/llm');
  await page.getByRole('button', { name: /Add provider/ }).first().click();
  await page.getByLabel('Name', { exact: true }).fill(`Fake LLM ${suffix}`);
  await page.getByLabel('Base URL').fill(INTERNAL_LLM + '/v1');
  await page.getByLabel('Model', { exact: true }).fill('fake-model');
  await page.getByLabel('Structured output').selectOption('prompt_only');
  await page.getByLabel('API key', { exact: true }).fill(`E2E-LLM-KEY-${suffix}`);
  await page.getByRole('checkbox', { name: /private network/i }).click();
  const confirm = page.getByRole('dialog');
  if (await confirm.isVisible()) await confirm.getByRole('button', { name: /Allow/ }).click();
  await page.getByRole('button', { name: /Save|Add|Create/ }).last().click();
  await expect(page.getByRole('heading', { name: `Fake LLM ${suffix}` })).toBeVisible();
  await expect(page.getByText(`E2E-LLM-KEY-${suffix}`)).toHaveCount(0);
  await page.getByRole('button', { name: 'Test connection' }).first().click();
  await expect(page.getByText('Connection successful; expected answer received.')).toBeVisible();
});

test('every page loads without console errors, exceptions or CSP violations', async () => {
  const paths = ['/', '/inbox', '/destinations', '/deliveries', '/settings', '/settings/llm'];
  for (const path of paths) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await page.waitForLoadState('networkidle');
  }
  // A project page (tokens, connect panel, routing) and a group page.
  await page.goto('/');
  await page.getByRole('button', { name: new RegExp(projectName) }).click();
  await expect(page.getByRole('heading', { name: 'Connect an application' })).toBeVisible();
  await page.goto('/inbox');
  await page.getByText(billingQuestion).first().click();
  await expect(page.getByRole('heading', { name: 'Occurrences' })).toBeVisible();
  await page.waitForLoadState('networkidle');
  await collectCsp(page, problems);
  expect(problems.consoleErrors).toEqual([]);
  expect(problems.pageErrors).toEqual([]);
  expect(problems.cspViolations).toEqual([]);
});

test('the sign-in page has no console errors or CSP violations', async ({ browser }) => {
  const fresh = await browser.newContext();
  const signedOut = await fresh.newPage();
  const signedOutProblems = await watchProblems(signedOut);
  await signedOut.goto('/');
  await expect(signedOut.getByRole('heading', { name: 'Sign in to your workspace' })).toBeVisible();
  await signedOut.waitForLoadState('networkidle');
  await collectCsp(signedOut, signedOutProblems);
  expect(signedOutProblems).toEqual({ consoleErrors: [], cspViolations: [], pageErrors: [] });
  await fresh.close();
});

test('keyboard only: sign in, open the inbox and a group, change its status', async ({ browser }) => {
  const fresh = await browser.newContext();
  const keys = await fresh.newPage();
  await keys.goto('/');
  await keys.getByLabel('Email address').focus();
  await keys.keyboard.type(process.env.E2E_ADMIN_EMAIL ?? 'e2e-admin@example.invalid');
  await keys.keyboard.press('Tab');
  await keys.keyboard.type(ADMIN_PASSWORD);
  await keys.keyboard.press('Enter');
  await expect(keys.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeFocused();

  // Tab to a control by its accessible name, giving up after a bounded number of presses.
  const tabTo = async (name: RegExp | string) => {
    for (let presses = 0; presses < 60; presses++) {
      await keys.keyboard.press('Tab');
      const label = await keys.evaluate(() => {
        const element = document.activeElement as HTMLElement | null;
        return (element?.getAttribute('aria-label') ?? element?.textContent ?? '').trim();
      });
      if (typeof name === 'string' ? label === name : name.test(label)) return;
    }
    throw new Error(`Could not reach ${String(name)} with the keyboard`);
  };
  await tabTo('Inbox');
  await keys.keyboard.press('Enter');
  await expect(keys.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeFocused();
  await tabTo(new RegExp(billingQuestion.slice(0, 20)));
  await keys.keyboard.press('Enter');
  await expect(keys.getByRole('heading', { level: 1 })).toContainText(billingQuestion);
  await tabTo('Group status');
  await keys.keyboard.type('In progress');
  await expect(keys.getByLabel('Group status')).toHaveValue('in_progress');
  await keys.reload();
  await expect(keys.getByLabel('Group status')).toHaveValue('in_progress');
  await fresh.close();
});
