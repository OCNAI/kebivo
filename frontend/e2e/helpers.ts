import { expect, type Page, type APIRequestContext } from '@playwright/test';

export const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? 'e2e-admin@example.invalid';
export const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? '';
const RECEIVER_URL = process.env.E2E_RECEIVER_URL ?? 'http://127.0.0.1:18080';
const LLM_URL = process.env.E2E_LLM_URL ?? 'http://127.0.0.1:18765';

/** Where the app reaches the test doubles, from inside the e2e network. */
export const INTERNAL_RECEIVER = 'http://runner:8080';
export const INTERNAL_LLM = 'http://runner:8765';

export async function signIn(page: Page, password = ADMIN_PASSWORD) {
  await page.goto('/');
  await page.getByLabel('Email address').fill(ADMIN_EMAIL);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

/** Resets the webhook receiver; the next `failFirst` posts answer 500. */
export async function resetReceiver(request: APIRequestContext, failFirst = 0) {
  const response = await request.delete(RECEIVER_URL + '/', { headers: { 'X-Fail-First': String(failFirst) } });
  expect(response.status()).toBe(204);
}

export async function receiverHits(request: APIRequestContext, path: string) {
  const rows = (await (await request.get(RECEIVER_URL + '/')).json()) as { path: string; body: string }[];
  return rows.filter((row) => row.path === path);
}

export async function setLlmScenario(request: APIRequestContext, scenario: string) {
  expect((await request.put(LLM_URL + '/scenario', { data: { scenario } })).ok()).toBe(true);
}

/** Submits a knowledge gap through the public REST endpoint, as an integration would. */
export async function submitGap(request: APIRequestContext, token: string, question: string, tags: string[]) {
  const response = await request.post('/api/v1/feedback', {
    headers: { Authorization: `Bearer ${token}` },
    data: { event_type: 'knowledge_gap', question, tags },
  });
  expect(response.status(), await response.text()).toBe(202);
}

export type PageProblems = { consoleErrors: string[]; cspViolations: string[]; pageErrors: string[] };

/**
 * Collects console errors, uncaught exceptions and CSP violations for a page.
 * The one tolerated message is the 401 that GET /auth/session returns before sign-in.
 */
export async function watchProblems(page: Page): Promise<PageProblems> {
  const problems: PageProblems = { consoleErrors: [], cspViolations: [], pageErrors: [] };
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (event) => {
      (window as unknown as { __csp: string[] }).__csp.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const url = message.location().url;
    if (url.endsWith('/api/v1/auth/session') && message.text().includes('401')) return;
    problems.consoleErrors.push(`${message.text()} (${url})`);
  });
  page.on('pageerror', (error) => problems.pageErrors.push(error.message));
  return problems;
}

export async function collectCsp(page: Page, problems: PageProblems) {
  const seen = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  problems.cspViolations.push(...seen);
}
