import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { ADMIN_PASSWORD, signIn, submitGap } from './helpers';
import { loginApi } from './seed';

test('first real report shows a quiet, durable star prompt in the labelled inbox', async ({ browser, baseURL }) => {
  test.setTimeout(90_000);
  const { api, admin } = await loginApi(baseURL!);
  expect(await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).toEqual({ show: false });

  const project = await admin.call<{ id: string }>('POST', '/projects', {
    name: 'First run acceptance', slug: `first-run-${Date.now()}`,
  });
  const token = (await admin.call<{ token: string }>('POST', `/projects/${project.id}/tokens`, {
    name: 'First run acceptance token',
  })).token;
  const question = `Where is the first run guide ${Date.now()}?`;
  await submitGap(api, token, question, ['first-run-acceptance']);
  await expect.poll(async () => (await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).show).toBe(true);
  await expect.poll(async () => {
    const groups = await admin.call<{ items: { representative: { question_excerpt: string } }[] }>(
      'GET', `/admin/groups?project_id=${project.id}`,
    );
    return groups.items.some(group => group.representative.question_excerpt === question);
  }, { timeout: 30_000 }).toBe(true);

  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const externalRequests: string[] = [];
  const origin = new URL(baseURL!).origin;
  page.on('request', request => {
    const url = request.url();
    if (/^https?:/.test(url) && new URL(url).origin !== origin) externalRequests.push(url);
  });
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await page.goto('/inbox');
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await page.getByText('More filters').click();
  await page.getByLabel('Filter by existing tag').selectOption({ label: 'first-run-acceptance' });
  await expect(page.getByText(question).first()).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toBeVisible();

  const screenshot = path.resolve(process.cwd(), '../artifacts/first-run/inbox-labelled.png');
  mkdirSync(path.dirname(screenshot), { recursive: true });
  await page.screenshot({ path: screenshot, fullPage: true });
  expect(externalRequests).toEqual([]);

  await page.getByRole('button', { name: 'Dismiss', exact: true }).click();
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);
  expect(await admin.call<{ show: boolean }>('GET', '/admin/star-prompt')).toEqual({ show: false });
  await page.reload();
  await expect(page.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);

  const newSession = await browser.newContext();
  const newPage = await newSession.newPage();
  await signIn(newPage, ADMIN_PASSWORD || '');
  await expect(newPage.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await newPage.goto('/inbox');
  await expect(newPage.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  await expect(newPage.getByRole('complementary', { name: 'Support Kebivo' })).toHaveCount(0);

  await newSession.close();
  await context.close();
  await api.dispose();
});
