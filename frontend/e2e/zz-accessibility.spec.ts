import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { signIn } from './helpers';
import { seed } from './seed';

test('setup, inbox, and group detail have no serious or critical axe violations', async ({ browser, page, baseURL }) => {
  const data = await seed(baseURL!);
  const axeResults: { page: string; violations: { id: string; impact: string | null; description: string; nodes: number }[] }[] = [];
  const check = async (target: Page, name: string) => {
    const results = await new AxeBuilder({ page: target }).analyze();
    axeResults.push({
      page: name,
      violations: results.violations.map(({ id, impact, description, nodes }) => ({ id, impact, description, nodes: nodes.length })),
    });
    expect(
      results.violations.filter(({ impact }) => impact === 'serious' || impact === 'critical'),
      `${name} should have no serious or critical accessibility violations`,
    ).toEqual([]);
  };

  // The e2e stack already has an administrator. Stub the setup status to inspect
  // the first-run form and its optional sample-data hint without creating an account.
  const setupContext = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const setupPage = await setupContext.newPage();
  await setupPage.route('**/api/v1/auth/setup', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ required: true }),
  }));
  await setupPage.goto('/setup#token=axe-check');
  await expect(setupPage.getByRole('heading', { name: 'Create your administrator account' })).toBeVisible();
  await check(setupPage, 'setup');
  await setupContext.close();

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
