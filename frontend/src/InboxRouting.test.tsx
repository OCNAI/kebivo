import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Inbox } from './Inbox';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

describe('group inbox rows', () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(() => {
    container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    window.history.replaceState({}, '', '/inbox');
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.includes('/admin/groups?')) return new Response(JSON.stringify({ items: [{ id: 'g1', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e1', question_excerpt: 'Where is the guide?' }, tags: [{ slug: 'it', name: 'IT' }], status: 'open', occurrence_count: 4, first_seen_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-02T00:00:00Z', delivery_summary: { delivered: 2, in_progress: 1, failed: 1 } }], next_cursor: null }));
      return new Response('{}');
    }) as typeof fetch;
  });
  afterEach(() => { vi.useRealTimers(); act(() => root.unmount()); container.remove(); window.history.replaceState({}, '', '/'); vi.restoreAllMocks(); });
  it('shows grouped question, status, occurrences, seen dates, tags and delivery totals', async () => {
    await act(async () => { root.render(<Inbox path="/inbox" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('Where is the guide?'); expect(container.textContent).toContain('Support'); expect(container.textContent).toContain('IT'); expect(container.textContent).toContain('Open'); expect(container.textContent).toContain('4'); expect(container.textContent).toContain('1 failed'); expect(container.textContent).not.toContain('2 delivered · 1 active · 1 failed');
    expect(container.querySelector('time[title]')).not.toBeNull();
  });
  it('explains the empty inbox and opens a project Connect panel', async () => {
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [{ id: 'p1', name: 'Support' }] }));
      if (url.includes('/admin/groups?')) return new Response(JSON.stringify({ items: [], next_cursor: null }));
      return new Response(JSON.stringify({ total_groups: 0, groups_by_status: {} }));
    }) as typeof fetch;
    const navigate = vi.fn();
    await act(async () => { root.render(<Inbox path="/inbox" navigate={navigate} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('Send a test event from a project Connect panel');
    await act(async () => { container.querySelector<HTMLButtonElement>('.empty-state button')!.click(); });
    expect(navigate).toHaveBeenCalledWith('/?project=p1');
  });
  it('renders question excerpts and tag names as literal text', async () => {
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.includes('/admin/groups?')) return new Response(JSON.stringify({ items: [{ id: 'g1', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e1', question_excerpt: '<script>alert(1)</script>' }, tags: [{ slug: 'unsafe', name: '<img onerror=alert(2)>' }], status: 'open', occurrence_count: 1, first_seen_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-02T00:00:00Z', delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } }], next_cursor: null }));
      return new Response('{}');
    }) as typeof fetch;
    await act(async () => { root.render(<Inbox path="/inbox" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('<script>alert(1)</script>');
    expect(container.textContent).toContain('<img onerror=alert(2)>');
    expect(container.querySelector('script, img')).toBeNull();
  });
  it('debounces search and replaces the current history entry', async () => {
    vi.useFakeTimers();
    const replace = vi.spyOn(window.history, 'replaceState');
    await act(async () => { root.render(<Inbox path="/inbox" navigate={() => {}} />); await vi.advanceTimersByTimeAsync(0); });
    const search = container.querySelector<HTMLInputElement>('input[aria-label="Search groups"]')!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'benefit'); search.dispatchEvent(new Event('input', { bubbles: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(299); });
    expect(replace).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(replace).toHaveBeenCalled();
    expect(window.location.search).toContain('q=benefit');
    vi.useRealTimers();
  });
  it('discards a load-more response after filters change', async () => {
    let finishPage!: (response: Response) => void;
    const pageResponse = new Promise<Response>(resolve => { finishPage = resolve; });
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith('/admin/groups/summary')) return new Response(JSON.stringify({ total_groups: 2, groups_by_status: { open: 2 } }));
      if (url.includes('cursor=')) return pageResponse;
      if (url.includes('/admin/groups?')) return new Response(JSON.stringify({ items: [{ id: 'g1', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e1', question_excerpt: 'First group' }, tags: [], status: 'open', occurrence_count: 1, first_seen_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-02T00:00:00Z', delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } }], next_cursor: 'next' }));
      return originalFetch(input);
    }) as typeof fetch;
    await act(async () => { root.render(<Inbox path="/inbox" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const more = Array.from(container.querySelectorAll('button')).find(button => button.textContent === 'Load more')!;
    await act(async () => { more.click(); });
    window.history.replaceState({}, '', '/inbox?event_type=knowledge_gap');
    await act(async () => { root.render(<Inbox path="/inbox" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => { finishPage(new Response(JSON.stringify({ items: [{ id: 'stale', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e2', question_excerpt: 'Stale group' }, tags: [], status: 'open', occurrence_count: 1, first_seen_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-02T00:00:00Z', delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } }], next_cursor: null }))); await pageResponse; });
    expect(container.textContent).not.toContain('Stale group');
  });
});
