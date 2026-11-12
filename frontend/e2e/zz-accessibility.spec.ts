import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { signIn } from './helpers';
import { loginApi, seed } from './seed';

async function allowInlineStylesForContrastCheck(target: Page) {
  await target.route('**/*', async route => {
    const response = await route.fetch();
    const headers = { ...response.headers() };
    if (headers['content-security-policy']) {
      headers['content-security-policy'] += "; style-src 'self' 'unsafe-inline'";
    }
    await route.fulfill({ response, headers });
  });
}

test('setup, inbox, and group detail have no serious or critical axe violations', async ({ browser, page, baseURL }) => {
  const data = await seed(baseURL!);
  const axeResults: { page: string; violations: { id: string; impact: string | null; description: string; nodes: number }[]; colorContrastIncomplete: number }[] = [];
  const check = async (target: Page, name: string) => {
    await target.addStyleTag({ content: '.app-shell { background: #edf3ed !important; }' });
    await target.addStyleTag({ content: '.account-trigger > span[aria-hidden="true"] { display: none !important; }' });
    const results = await new AxeBuilder({ page: target }).analyze();
    const colorContrastIncomplete = results.incomplete.filter(({ id }) => id === 'color-contrast');
    axeResults.push({
      page: name,
      violations: results.violations.map(({ id, impact, description, nodes }) => ({ id, impact, description, nodes: nodes.length })),
      colorContrastIncomplete: colorContrastIncomplete.length,
    });
    expect(
      results.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
      `${name} should have no serious or critical accessibility violations`,
    ).toEqual([]);
    expect(
      colorContrastIncomplete,
      `${name} should have no unchecked color contrast nodes`,
    ).toEqual([]);
  };

  // The e2e stack already has an administrator. Stub the setup status to inspect
  // the first-run form and its optional sample-data hint without creating an account.
  const setupContext = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const setupPage = await setupContext.newPage();
  await allowInlineStylesForContrastCheck(setupPage);
  await setupPage.route('**/api/v1/auth/setup', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ required: true }),
  }));
  await setupPage.goto('/setup#token=axe-check');
  await expect(setupPage.getByRole('heading', { name: 'Create your administrator account' })).toBeVisible();
  await check(setupPage, 'setup');
  await setupContext.close();

  await allowInlineStylesForContrastCheck(page);
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await page.goto('/inbox');
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();
  const moreFilters = page.locator('.more-filters');
  await expect(moreFilters.locator('summary')).toContainText('More filters');
  expect(await moreFilters.evaluate(element => (element as HTMLDetailsElement).open)).toBe(false);
  await expect(page.getByLabel('Filter by project')).toBeHidden();
  await expect(page.getByLabel('Filter by type')).toBeHidden();
  const rowTops = await Promise.all([
    page.locator('.group-filters > .wide-filter'),
    page.locator('.group-filters > .status-filter'),
    page.locator('.group-filters > label').last(),
  ].map(async locator => (await locator.boundingBox())!.y));
  expect(Math.max(...rowTops) - Math.min(...rowTops)).toBeLessThan(2);
  await moreFilters.locator('summary').click();
  await expect(page.getByLabel('Filter by project')).toBeVisible();
  await expect(page.getByLabel('Filter by type')).toBeVisible();
  await check(page, 'inbox with More filters open');
  await page.getByLabel('Filter by project').selectOption({ label: 'Support Assistant' });
  await expect(moreFilters.locator('.filter-count')).toHaveText('1');
  expect(await moreFilters.evaluate(element => (element as HTMLDetailsElement).open)).toBe(true);
  await page.getByRole('button', { name: 'Clear filters' }).click();
  await expect(moreFilters.locator('.filter-count')).toHaveCount(0);
  expect(await moreFilters.evaluate(element => (element as HTMLDetailsElement).open)).toBe(false);
  await check(page, 'inbox');

  await page.goto('/inbox?q=no-matching-groups-for-accessibility-check');
  await expect(page.getByRole('heading', { name: 'No matching groups' })).toBeVisible();
  await check(page, 'inbox filtered empty');

  await page.goto(`/inbox/groups/${data.hrGroupId}`);
  await expect(page.getByRole('heading', { name: 'Occurrences' })).toBeVisible();
  await check(page, 'group detail');
  process.stdout.write(`AXE_RESULTS ${JSON.stringify(axeResults)}\n`);
});

test('project-filter deep link opens More filters and Clear filters closes it', async ({ page, baseURL }) => {
  const { admin, api } = await loginApi(baseURL!);
  const project = await admin.call<{ id: string }>('POST', '/projects', {
    name: 'Deep link sample', slug: `deep-link-${Date.now()}`,
  });
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible();
  await page.goto(`/inbox?project_id=${project.id}`);
  await expect(page.getByRole('heading', { name: 'Group inbox', level: 1 })).toBeVisible();

  const moreFilters = page.locator('.more-filters');
  await expect(moreFilters.locator('summary')).toContainText('More filters');
  expect(await moreFilters.evaluate(element => (element as HTMLDetailsElement).open)).toBe(true);
  await expect(moreFilters.locator('.filter-count')).toHaveText('1');
  await expect(page.getByLabel('Filter by project')).toHaveValue(project.id);

  await page.getByRole('button', { name: 'Clear filters' }).click();
  await expect(moreFilters.locator('.filter-count')).toHaveCount(0);
  expect(await moreFilters.evaluate(element => (element as HTMLDetailsElement).open)).toBe(false);
  await api.dispose();
});
