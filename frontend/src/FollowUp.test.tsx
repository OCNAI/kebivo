import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { DashboardSummary } from './DashboardSummary';
import { GroupDetail } from './GroupDetail';
import { Inbox } from './Inbox';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

const now = Date.now();
const daysAgo = (days: number) => new Date(now - days * 86400000).toISOString();
const base = { project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', tags: [], occurrence_count: 3, first_seen_at: daysAgo(10), last_seen_at: daysAgo(1), delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } };
const quiet = { ...base, id: 'g1', representative: { event_id: 'e1', question_excerpt: 'Do you ship to Canada?' }, status: 'resolved', resolved_at: daysAgo(3), reopened: null, resolution_note: { id: 'n1', body: 'Added Canada to the FAQ' } };
const back = { ...base, id: 'g2', representative: { event_id: 'e2', question_excerpt: 'Can I return an item?' }, status: 'open', resolved_at: null, reopened: { reopened_at: daysAgo(1), resolved_at: '2026-11-10T12:00:00Z', new_occurrences: 2 }, resolution_note: null };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

describe('whether a fix worked', () => {
  let root: Root;
  let container: HTMLDivElement;
  let requests: { url: string; method: string; body?: unknown }[];
  let group: Record<string, unknown>;
  beforeEach(() => {
    container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    requests = [];
    group = { ...quiet, status: 'open', resolved_at: null, resolution_note: null, representative: { event_id: 'e1', question: 'Do you ship to Canada?' }, notes: [] };
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input), method = init?.method ?? 'GET';
      requests.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.includes('/admin/groups/summary')) return new Response(JSON.stringify({ total_groups: 2, total_events: 6, groups_by_status: { open: 1, resolved: 1 }, reopened: 1, failed_deliveries_last_24h: 0, last_7d_from: '2026-11-09T00:00:00+00:00', resolved_last_7d: 4, reopened_last_7d: 1, top_open_gaps: [] }));
      if (url.includes('/admin/groups?')) return new Response(JSON.stringify({ items: [quiet, back], next_cursor: null }));
      if (url.includes('/admin/groups/g1/') || url.includes('/deliveries?')) {
        if (method === 'POST') group = { ...group, resolution_note: { id: 'n2', body: 'Linked the shipping page' } };
        return new Response(JSON.stringify({ items: [], next_cursor: null }));
      }
      if (url.endsWith('/admin/groups/g1')) {
        if (method === 'PATCH') group = { ...group, status: 'resolved', resolved_at: new Date().toISOString() };
        return new Response(JSON.stringify(group));
      }
      return new Response('{}');
    }) as typeof fetch;
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); window.history.replaceState({}, '', '/'); vi.restoreAllMocks(); });

  it('shows quiet and reopened groups in the inbox and filters reopened ones', async () => {
    window.history.replaceState({}, '', '/inbox');
    const navigate = vi.fn((url: string) => window.history.pushState({}, '', url));
    await act(async () => { root.render(<Inbox path="/inbox" navigate={navigate} />); await tick(); });
    const rows = [...container.querySelectorAll('tbody tr')].map(row => row.textContent ?? '');
    expect(rows[0]).toContain('Resolved 3 days ago, no new occurrences since');
    expect(rows[0]).toContain('You changed: Added Canada to the FAQ');
    expect(rows[1]).toContain('Reopened');
    expect(rows[1]).toContain(`2 new since you resolved it on ${new Date('2026-11-10T12:00:00Z').toLocaleDateString()}`);
    const toggle = [...container.querySelectorAll<HTMLButtonElement>('.status-toggle')].find(button => button.textContent?.startsWith('Reopened'))!;
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.textContent).toBe('Reopened 1');
    await act(async () => { toggle.click(); await tick(); });
    expect(navigate).toHaveBeenCalledWith('/inbox?reopened=true');
    await act(async () => { root.render(<Inbox path="/inbox?reopened=true" navigate={navigate} />); await tick(); });
    expect(requests.some(request => request.url.includes('/admin/groups?reopened=true'))).toBe(true);
    expect(container.querySelector('button[aria-label="Remove reopened filter"]')).not.toBeNull();
    // The existing status toggles are unchanged.
    expect([...container.querySelectorAll('.status-toggle')].map(button => button.textContent)).toEqual(['Open 1', 'In progress 0', 'Resolved 1', 'Dismissed 0', 'Reopened 1']);
  });

  it('links the 7-day tiles to the filtered inbox', async () => {
    const navigate = vi.fn();
    await act(async () => { root.render(<DashboardSummary navigate={navigate} />); await tick(); });
    const tile = (label: string) => [...container.querySelectorAll('a')].find(link => link.textContent?.endsWith(label))!;
    expect(tile('Resolved (7d)').textContent).toBe('4Resolved (7d)');
    expect(tile('Reopened (7d)').getAttribute('href')).toBe('/inbox?reopened=true&status_changed_from=2026-11-09T00%3A00%3A00%2B00%3A00');
    await act(async () => { tile('Resolved (7d)').click(); });
    expect(navigate).toHaveBeenCalledWith('/inbox?status=resolved&status_changed_from=2026-11-09T00%3A00%3A00%2B00%3A00');
  });

  it('resolves in one action and saves an optional change note', async () => {
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await tick(); });
    const resolved = [...container.querySelectorAll<HTMLButtonElement>('.group-status button')].find(button => button.textContent === 'Resolved')!;
    await act(async () => { resolved.click(); await tick(); await tick(); });
    expect(requests.filter(request => request.method === 'PATCH').map(request => request.body)).toEqual([{ status: 'resolved' }]);
    expect(container.querySelector('h1 + button + .group-chips + .follow-up')?.textContent).toContain('no new occurrences since');
    const input = container.querySelector<HTMLInputElement>('#resolve-change')!;
    expect(container.querySelector('label[for="resolve-change"]')?.textContent).toBe('What did you change? (optional)');
    expect(document.activeElement).toBe(input);
    expect(input.form!.hasAttribute('aria-label')).toBe(false);
    expect(resolved.getAttribute('aria-pressed')).toBe('true');
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'Linked the shipping page'); input.dispatchEvent(new Event('input', { bubbles: true })); });
    await act(async () => { input.form!.requestSubmit(); await tick(); await tick(); });
    expect(requests.find(request => request.method === 'POST')?.body).toEqual({ body: 'Linked the shipping page', resolution: true });
    expect(container.querySelector('#resolve-change')).toBeNull();
    expect(container.textContent).toContain('You changed: Linked the shipping page');
  });

  it('can skip the change note', async () => {
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await tick(); });
    const resolved = [...container.querySelectorAll<HTMLButtonElement>('.group-status button')].find(button => button.textContent === 'Resolved')!;
    await act(async () => { resolved.click(); await tick(); await tick(); });
    await act(async () => { [...container.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Skip')!.click(); });
    expect(container.querySelector('#resolve-change')).toBeNull();
    expect(requests.some(request => request.method === 'POST')).toBe(false);
    expect(container.textContent).toContain('no new occurrences since');
  });
});
