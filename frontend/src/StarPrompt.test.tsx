import { afterEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StarPrompt } from './Inbox';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

describe('GitHub star prompt', () => {
  let container: HTMLDivElement;
  let root: Root;
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); });

  async function render(show: boolean) {
    container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    const calls: { path: string; method: string }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input); const method = init?.method ?? 'GET'; calls.push({ path, method });
      if (path.endsWith('/admin/star-prompt') && method === 'GET') return new Response(JSON.stringify({ show }));
      if (path.endsWith('/admin/star-prompt/dismiss') && method === 'POST') return new Response(null, { status: 204 });
      return new Response('{}');
    });
    await act(async () => { root.render(<StarPrompt />); await new Promise(resolve => setTimeout(resolve, 0)); });
    return calls;
  }

  it('stays hidden when the server says show is false', async () => {
    const calls = await render(false);
    expect(container.textContent).toBe('');
    expect(calls).toContainEqual({ path: '/api/v1/admin/star-prompt', method: 'GET' });
  });

  it('dismisses immediately and posts the dismissal', async () => {
    const calls = await render(true);
    await act(async () => { container.querySelector<HTMLButtonElement>('button')!.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toBe('');
    expect(calls).toContainEqual({ path: '/api/v1/admin/star-prompt/dismiss', method: 'POST' });
  });

  it('hides and posts when the GitHub link is followed', async () => {
    const calls = await render(true);
    const link = container.querySelector<HTMLAnchorElement>('a')!;
    expect(link.href).toBe('https://github.com/OCNAI/kebivo');
    expect(link.target).toBe('_blank');
    expect(link.rel).toBe('noopener noreferrer');
    await act(async () => { link.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toBe('');
    expect(calls).toContainEqual({ path: '/api/v1/admin/star-prompt/dismiss', method: 'POST' });
  });
});
