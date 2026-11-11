import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FirstRunEmpty, FirstRunProvider, SetupPage, takeSetupTokenFromLocation } from './FirstRun';
import { setCsrfToken } from './api';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const originalFetch = globalThis.fetch;
function setInput(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

describe('first run setup', () => {
  let root: Root;
  let host: HTMLDivElement;
  beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host); });
  afterEach(() => { act(() => root.unmount()); host.remove(); setCsrfToken(null); globalThis.fetch = originalFetch; vi.restoreAllMocks(); });

  it('clears the setup fragment before returning the in-memory token', () => {
    history.replaceState({}, '', '/setup?keep=yes#token=secret-value');
    const replace = vi.spyOn(history, 'replaceState');
    expect(takeSetupTokenFromLocation()).toBe('secret-value');
    expect(replace).toHaveBeenCalledWith({}, '', '/setup?keep=yes');
    expect(location.hash).toBe('');
    expect(location.search).toBe('?keep=yes');
  });

  it('renders the form, validates both fields, and keeps a sample failure out of setup failure', async () => {
    const onSuccess = vi.fn();
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input);
      if (path.endsWith('/auth/setup') && init?.method === 'GET') return new Response(JSON.stringify({ required: true }));
      if (path.endsWith('/auth/setup')) return new Response(JSON.stringify({ admin: { id: 'a', email: 'person@example.com' }, csrf_token: 'csrf' }));
      if (path.endsWith('/admin/sample-data')) return new Response(JSON.stringify({ detail: 'temporary failure' }), { status: 500 });
      return new Response('{}');
    }) as typeof fetch;
    await act(async () => { root.render(<SetupPage token="memory-only" onSuccess={onSuccess} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain('Create your administrator account');
    expect(host.querySelectorAll<HTMLInputElement>('input[type=checkbox]')[1].checked).toBe(true);
    await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(host.textContent).toContain('Fix 2 problems to continue.');
    expect(host.textContent).toContain('Enter a valid email address.');
    expect(host.textContent).toContain('Use at least 12 characters.');
    await act(async () => {
      const email = host.querySelector<HTMLInputElement>('#setup-email')!, password = host.querySelector<HTMLInputElement>('#setup-password')!;
      setInput(email, 'person@example.com'); setInput(password, 'correct horse battery');
      host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect(onSuccess).toHaveBeenCalledWith({ id: 'a', email: 'person@example.com' }, 'csrf', false, true);
  });

  it('shows the invalid-link card when the server rejects the token', async () => {
    globalThis.fetch = vi.fn(async (_input, init) => init?.method === 'POST'
      ? new Response(JSON.stringify({ detail: 'Invalid or expired setup link' }), { status: 403 })
      : new Response(JSON.stringify({ required: true }))) as typeof fetch;
    await act(async () => { root.render(<SetupPage token="memory-only" onSuccess={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const email = host.querySelector<HTMLInputElement>('#setup-email')!, password = host.querySelector<HTMLInputElement>('#setup-password')!;
    await act(async () => {
      setInput(email, 'person@example.com'); setInput(password, 'correct horse battery');
      host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect(host.textContent).toContain("This setup link doesn't work");
    expect(host.textContent).toContain('Just reloaded this page?');
  });

  it('offers Retry when setup is unreachable and shows the already-set-up state after recovery', async () => {
    let attempts = 0;
    globalThis.fetch = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error('offline');
      return new Response(JSON.stringify({ required: false }));
    }) as typeof fetch;
    await act(async () => { root.render(<SetupPage token={null} onSuccess={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("Couldn't reach Kebivo");
    await act(async () => { [...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Retry')!.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain('This server is already set up');
  });
});

describe('sample banner removal', () => {
  let root: Root;
  let host: HTMLDivElement;
  const originalShowModal = HTMLDialogElement.prototype.showModal;
  const originalClose = HTMLDialogElement.prototype.close;
  beforeEach(() => {
    host = document.createElement('div'); document.body.append(host); root = createRoot(host);
    HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
    HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new Event('close')); };
  });
  afterEach(() => {
    act(() => root.unmount()); host.remove(); globalThis.fetch = originalFetch; vi.restoreAllMocks();
    HTMLDialogElement.prototype.showModal = originalShowModal; HTMLDialogElement.prototype.close = originalClose;
  });

  it('opens confirmation, supports Keep it, and removes through the API', async () => {
    let present = true;
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${path}`);
      if (path.endsWith('/admin/sample-data') && method === 'GET') return new Response(JSON.stringify({ present, has_real_events: false, has_real_projects: false }));
      if (path.endsWith('/admin/sample-data') && method === 'DELETE') { present = false; return new Response(null, { status: 204 }); }
      return new Response('{}');
    }) as typeof fetch;
    const navigate = vi.fn();
    await act(async () => { root.render(<FirstRunProvider navigate={navigate}><main><h1 tabIndex={-1}>Inbox</h1></main></FirstRunProvider>); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(host.textContent).toContain("You're looking at sample data.");
    const remove = [...host.querySelectorAll('button')].find(button => button.textContent === 'Remove sample data')!;
    await act(async () => { remove.click(); });
    expect(host.querySelector('dialog')?.hasAttribute('open')).toBe(true);
    await act(async () => { host.querySelector<HTMLButtonElement>('dialog button')!.click(); });
    expect(host.querySelector('dialog')?.hasAttribute('open')).toBe(false);
    await act(async () => { remove.click(); });
    const destructive = [...host.querySelectorAll<HTMLButtonElement>('dialog button')].find(button => button.textContent === 'Remove sample data')!;
    await act(async () => { destructive.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(calls).toContain('DELETE /api/v1/admin/sample-data');
    expect(navigate).toHaveBeenCalledWith('/inbox');
    expect(host.textContent).not.toContain("You're looking at sample data.");
    expect(host.textContent).toContain('Sample data removed.');
  });

  it('keeps the sample action available when sample status cannot be fetched', async () => {
    globalThis.fetch = vi.fn(async input => {
      const path = String(input);
      if (path.endsWith('/admin/sample-data')) throw new Error('offline');
      if (path.endsWith('/admin/groups/summary')) return new Response(JSON.stringify({ total_events: 0 }));
      return new Response('{}');
    }) as typeof fetch;
    await act(async () => { root.render(<FirstRunProvider navigate={() => {}}><main><h1>Dashboard</h1><FirstRunEmpty dashboard navigate={() => {}} /></main></FirstRunProvider>); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect([...host.querySelectorAll<HTMLButtonElement>('button')].some(button => button.textContent === 'Load sample data')).toBe(true);
  });
});
