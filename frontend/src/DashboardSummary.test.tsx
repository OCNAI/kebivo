import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { DashboardSummary } from './DashboardSummary';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

describe('Dashboard delivery failure link', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({
      total_groups: 0,
      total_events: 0,
      groups_by_status: {},
      failed_deliveries_last_24h: 7,
      top_open_gaps: [],
    }))) as typeof fetch;
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it('links to the failed status filter used by Deliveries', async () => {
    const navigate = vi.fn();
    await act(async () => {
      root.render(<DashboardSummary navigate={navigate} />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const failureLink = [...container.querySelectorAll('a')].find((link) =>
      link.textContent?.includes('Delivery failures (24h)'),
    );
    expect(failureLink?.getAttribute('href')).toBe('/deliveries?status=failed');

    await act(async () => failureLink?.click());
    expect(navigate).toHaveBeenCalledWith('/deliveries?status=failed');

    const deliveriesUrl = new URL(failureLink!.href);
    expect(deliveriesUrl.searchParams.get('status')).toBe('failed');
  });

  it('shows only recurring questions in a compact four-column table', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({
      total_groups: 2,
      total_events: 4,
      groups_by_status: { open: 2, in_progress: 0 },
      failed_deliveries_last_24h: 0,
      top_open_gaps: [
        { id: 'g1', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e1', question_excerpt: 'Repeated question' }, tags: [], status: 'open', occurrence_count: 3, first_seen_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-03T00:00:00Z', delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } },
        { id: 'g2', project: { id: 'p1', name: 'Support', slug: 'support' }, event_type: 'knowledge_gap', representative: { event_id: 'e2', question_excerpt: 'One-off question' }, tags: [], status: 'open', occurrence_count: 1, first_seen_at: '2026-01-02T00:00:00Z', last_seen_at: '2026-01-02T00:00:00Z', delivery_summary: { delivered: 0, in_progress: 0, failed: 0 } },
      ],
    }))) as typeof fetch;
    await act(async () => {
      root.render(<DashboardSummary navigate={() => {}} />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.querySelectorAll('.recurring-table thead th')).toHaveLength(4);
    expect(container.textContent).toContain('Repeated question');
    expect(container.textContent).not.toContain('One-off question');
  });

  it('explains when no question has recurred', async () => {
    await act(async () => {
      root.render(<DashboardSummary navigate={() => {}} />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.textContent).toContain('No question has been reported more than once yet.');
  });
});
