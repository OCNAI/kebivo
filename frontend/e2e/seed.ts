import { expect, request as playwrightRequest, type APIRequestContext } from '@playwright/test';
import { ADMIN_EMAIL, ADMIN_PASSWORD, INTERNAL_LLM, INTERNAL_RECEIVER, resetReceiver, setLlmScenario, submitGap } from './helpers';

/**
 * Synthetic data for the README screenshots (docs/images). Everything here is made up:
 * example.org addresses, no real names. Used only by screenshots.spec.ts, on a fresh stack.
 */
export type Seeded = { supportProjectId: string; failedDeliveryId: string; hrGroupId: string };

class Admin {
  constructor(private api: APIRequestContext, private csrf: string) {}
  private headers() {
    return { 'X-CSRF-Token': this.csrf };
  }
  async call<T = unknown>(method: 'GET' | 'POST' | 'PATCH' | 'PUT', path: string, data?: unknown): Promise<T> {
    const response = await this.api.fetch(`/api/v1${path}`, { method, data, headers: this.headers() });
    expect(response.ok(), `${method} ${path}: ${await response.text()}`).toBe(true);
    return (response.status() === 204 ? undefined : await response.json()) as T;
  }
}

export async function loginApi(baseURL: string) {
  const api = await playwrightRequest.newContext({ baseURL, extraHTTPHeaders: { Origin: baseURL } });
  const response = await api.post('/api/v1/auth/login', { data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD } });
  expect(response.ok()).toBe(true);
  const csrf = ((await response.json()) as { csrf_token: string }).csrf_token;
  return { api, admin: new Admin(api, csrf) };
}

type Id = { id: string };

export async function seed(baseURL: string): Promise<Seeded> {
  const { api, admin } = await loginApi(baseURL);
  const bare = await playwrightRequest.newContext({ baseURL });
  await setLlmScenario(bare, 'no_match');
  await resetReceiver(bare, 0);

  const provider = await admin.call<Id>('POST', '/admin/llm-providers', {
    name: 'Local model', provider_type: 'openai_compatible', base_url: INTERNAL_LLM + '/v1', model: 'local-model',
    allow_private_network: true, timeout_seconds: 5, structured_output: 'prompt_only', api_key: 'synthetic-key-not-a-secret',
  });
  const support = await admin.call<Id>('POST', '/projects', { name: 'Support Assistant', slug: 'support-assistant' });
  const hr = await admin.call<Id>('POST', '/projects', { name: 'HR Helpdesk', slug: 'hr-helpdesk' });
  await admin.call('PUT', `/projects/${support.id}/default-tags`, { tags: ['it', 'hr', 'billing'] });
  await admin.call('PATCH', `/projects/${hr.id}`, { settings: { deduplication: {
    mode: 'semantic', privacy_acknowledged: true,
    semantic: { provider_id: provider.id, min_confidence: 0.9, timeout_seconds: 5, max_attempts: 1 },
  } } });

  const webhook = await admin.call<Id>('POST', '/destinations', {
    name: 'IT webhook', type: 'webhook', allow_private_network: true,
    config: { url: INTERNAL_RECEIVER + '/it', signing: true, auth: { type: 'none' } },
  });
  const mail = await admin.call<Id>('POST', '/destinations', {
    name: 'Service desk mail', type: 'smtp', allow_private_network: true,
    config: { host: 'runner', port: 2525, tls_mode: 'none', from: 'kebivo@example.org', to: ['service-desk@example.org'] },
  });
  const destinations = await admin.call<{ items: (Id & { is_system: boolean })[] }>('GET', '/destinations');
  const inbox = destinations.items.find((item) => item.is_system)!;
  const rule = (name: string, tag: string, destination: string, priority: number) => ({
    name, event_types: ['knowledge_gap'], match_mode: 'any', tags: [tag], priority, destination_ids: [destination],
    match_behavior: 'continue',
  });
  await admin.call('POST', `/projects/${support.id}/routing-rules`, rule('IT questions to the webhook', 'it', webhook.id, 10));
  await admin.call('POST', `/projects/${support.id}/routing-rules`, rule('Billing questions to the service desk', 'billing', mail.id, 20));
  await admin.call('PATCH', `/projects/${support.id}`, { fallback_destination_id: inbox.id });

  const token = async (projectId: string) => {
    const created = await admin.call<{ token: string }>('POST', `/projects/${projectId}/tokens`, { name: 'Production integration' });
    return created.token;
  };
  const supportToken = await token(support.id);
  const hrToken = await token(hr.id);

  const send = async (tok: string, question: string, tags: string[], times = 1) => {
    for (let i = 0; i < times; i++) await submitGap(bare, tok, i % 2 ? question.replace(/\?$/, '') : question, tags);
  };
  // Recurring and single questions for Support Assistant (tag `it` goes to the webhook, `billing` to mail).
  await send(supportToken, 'How do I reset my VPN password?', ['it', 'vpn'], 6);
  await send(supportToken, 'Why was I charged twice this month?', ['billing'], 4);
  await send(supportToken, 'Where can I find the expense policy?', ['hr'], 3);
  await send(supportToken, 'How do I connect to the office printer?', ['it', 'printer'], 2);
  await send(supportToken, 'Can I get an invoice in euros?', ['billing']);
  await send(supportToken, 'Is the VPN client available for Linux?', ['it', 'vpn']);
  await send(supportToken, 'How do I change my shipping address?', []);
  await send(supportToken, 'What is the notice period for annual leave?', ['hr']);
  await send(supportToken, 'Which browser versions are supported?', ['it']);

  // HR Helpdesk: one question that the local model reports as the same issue (a semantic match).
  await send(hrToken, 'How many vacation days do I get each year?', ['leave'], 3);
  await setLlmScenario(bare, 'valid');
  await send(hrToken, 'What is my annual leave allowance?', ['leave']);
  await setLlmScenario(bare, 'no_match');
  await send(hrToken, 'How do I enrol in the pension plan?', ['benefits'], 2);
  await send(hrToken, 'When is the next payroll date?', ['payroll']);

  // Deliveries: one failure that is retried to success, then one that stays failed.
  const waitForDelivery = async (question: string, status: string) => {
    let found: { id: string } | undefined;
    await expect.poll(async () => {
      const page = (await admin.call<{ items: (Id & { status: string; event_summary?: string })[] }>('GET', `/deliveries?destination_id=${webhook.id}&status=${status}&limit=100`)).items;
      found = page.find((item) => !seenDeliveries.has(item.id));
      return Boolean(found);
    }, { timeout: 60_000, message: question }).toBe(true);
    seenDeliveries.add(found!.id);
    return found!;
  };
  const seenDeliveries = new Set<string>();
  const existing = await admin.call<{ items: Id[] }>('GET', `/deliveries?destination_id=${webhook.id}&limit=100`);
  existing.items.forEach((item) => seenDeliveries.add(item.id));

  await resetReceiver(bare, 100);
  await send(supportToken, 'Why does the VPN drop every hour?', ['it', 'vpn']);
  const recovered = await waitForDelivery('first failure', 'failed');
  await resetReceiver(bare, 0);
  await admin.call('POST', `/deliveries/${recovered.id}/retry`, {});
  await resetReceiver(bare, 100);
  await send(supportToken, 'How do I pair a new laptop with the VPN?', ['it', 'vpn']);
  const failed = await waitForDelivery('second failure', 'failed');

  // Triage: a mix of statuses, notes and a tag on the biggest recurring group.
  const groups = await admin.call<{ items: (Id & { representative: { question_excerpt: string } })[] }>('GET', '/admin/groups?limit=100');
  const byQuestion = (text: string) => groups.items.find((group) => group.representative.question_excerpt.startsWith(text))!;
  const vpn = byQuestion('How do I reset my VPN');
  await admin.call('PATCH', `/admin/groups/${vpn.id}`, { status: 'in_progress' });
  await admin.call('POST', `/admin/groups/${vpn.id}/notes`, { body: 'Reset link expires after 15 minutes. Add this to the VPN guide.' });
  await admin.call('POST', `/admin/groups/${vpn.id}/notes`, { body: 'Waiting for the IT team to confirm the Linux client.' });
  await admin.call('PATCH', `/admin/groups/${vpn.id}/tags`, { add: ['needs-docs'], remove: [] });
  await admin.call('PATCH', `/admin/groups/${byQuestion('Where can I find the expense').id}`, { status: 'resolved' });
  await admin.call('PATCH', `/admin/groups/${byQuestion('Can I get an invoice').id}`, { status: 'resolved' });
  await admin.call('PATCH', `/admin/groups/${byQuestion('Which browser versions').id}`, { status: 'dismissed' });
  const hrGroup = byQuestion('How many vacation days');
  await admin.call('POST', `/admin/groups/${hrGroup.id}/notes`, { body: 'Matches the annual leave policy, section 3.' });
  await admin.call('POST', `/admin/groups/${hrGroup.id}/notes`, { body: 'Answer added to the HR handbook.' });
  await admin.call('PATCH', `/admin/groups/${hrGroup.id}/tags`, { add: ['policy'], remove: [] });

  await bare.dispose();
  await api.dispose();
  return { supportProjectId: support.id, failedDeliveryId: failed.id, hrGroupId: hrGroup.id };
}
