import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Deliveries } from './Deliveries';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

const failed = {
  id: 'delivery-1', event_id: 'event-1', project_id: 'project-1', destination_id: 'hook-1',
  status: 'failed', attempt_count: 1, last_error_category: 'http_5xx', next_attempt_at: null,
  created_at: '2026-01-01T00:00:00Z',
  attempts: [{ attempt_number: 1, trigger: 'auto', started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:00:01Z', duration_ms: 1000, result: 'retryable_failure', http_status: 500, error_category: 'http_5xx', sanitized_error: 'Destination returned a retryable response' }],
};
const delivered = { ...failed, status: 'delivered', attempt_count: 2, attempts: [...failed.attempts, { ...failed.attempts[0], attempt_number: 2, trigger: 'manual', result: 'success', http_status: 200, error_category: null, sanitized_error: null }] };

describe('Delivery log UI', () => {
  let root: Root;
  let container: HTMLDivElement;
  let retried = false;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    retried = false;
    window.history.replaceState({}, '', '/deliveries?delivery_id=delivery-1');
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/destinations')) return new Response(JSON.stringify({ items: [{ id: 'hook-1', name: 'Hook', enabled: true }] }));
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.includes('/deliveries?') && url.includes('limit=30')) return new Response(JSON.stringify({ items: [retried ? delivered : failed], next_cursor: null }));
      if (url.endsWith('/deliveries/delivery-1/retry') && init?.method === 'POST') { retried = true; return new Response(JSON.stringify(delivered)); }
      if (url.endsWith('/deliveries/delivery-1')) return new Response(JSON.stringify(retried ? delivered : failed));
      return new Response('{}');
    }) as typeof fetch;
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    window.history.replaceState({}, '', '/');
    vi.restoreAllMocks();
  });

  it('shows sanitized attempts and retries an enabled failed delivery accessibly', async () => {
    await act(async () => { root.render(<Deliveries navigate={() => {}} />); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('Destination returned a retryable response');
    expect(container.querySelector('button[disabled]')).toBeNull();
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Retry');
    expect(retry).toBeTruthy();
    await act(async () => { retry!.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(retried).toBe(true);
    expect(container.textContent).toContain('Retry finished: delivered.');
    expect(container.textContent).toContain('Trigger: manual');
    expect(container.textContent).toContain('Result: success');
  });

  it('shows the failed filter selected when status=failed is in the URL', async () => {
    window.history.replaceState({}, '', '/deliveries?status=failed');
    await act(async () => { root.render(<Deliveries navigate={() => {}} />); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(container.querySelector<HTMLSelectElement>('select[aria-label="Filter by state"]')?.value).toBe('failed');
  });

  it('renders duplicate suppression in plain language', async () => {
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith('/destinations')) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [] }));
      if (url.includes('/deliveries?')) return new Response(JSON.stringify({ items: [{ ...failed, status: 'suppressed_duplicate' }], next_cursor: null }));
      return new Response('{}');
    }) as typeof fetch;
    window.history.replaceState({}, '', '/deliveries');
    await act(async () => { root.render(<Deliveries navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('Duplicate suppressed · already sent for this group');
  });

  it('explains when retention removed every attempt detail while keeping delivery totals', async () => {
    globalThis.fetch = vi.fn(async input => {
      const url=String(input);
      if(url.endsWith('/destinations'))return new Response(JSON.stringify({items:[]}));
      if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[]}));
      if(url.endsWith('/deliveries/delivery-1'))return new Response(JSON.stringify({...failed,attempt_count:3,attempts:[]}));
      if(url.includes('/deliveries?'))return new Response(JSON.stringify({items:[{...failed,attempt_count:3,attempts:[]}],next_cursor:null}));
      return new Response('{}');
    }) as typeof fetch;
    await act(async()=>{root.render(<Deliveries navigate={()=>{}}/>);await new Promise(resolve=>setTimeout(resolve,0));});
    expect(container.textContent).toContain('Attempt details removed by retention');
    expect(container.textContent).toContain('Attempts: 3');
    expect(container.textContent).not.toContain('No attempts recorded');
  });

  it('identifies older attempts pruned before a later retry', async () => {
    globalThis.fetch=vi.fn(async input=>{const url=String(input);if(url.endsWith('/destinations'))return new Response(JSON.stringify({items:[]}));if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[]}));if(url.endsWith('/deliveries/delivery-1'))return new Response(JSON.stringify({...failed,attempt_count:4,attempts:[{...failed.attempts[0],attempt_number:4}]}));if(url.includes('/deliveries?'))return new Response(JSON.stringify({items:[{...failed,attempt_count:4}],next_cursor:null}));return new Response('{}');}) as typeof fetch;
    await act(async()=>{root.render(<Deliveries navigate={()=>{}}/>);await new Promise(resolve=>setTimeout(resolve,0));});
    expect(container.textContent).toContain('Attempts 1–3 removed by retention.');
  });
});
