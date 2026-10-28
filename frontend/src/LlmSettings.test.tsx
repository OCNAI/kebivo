import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { LlmSettings } from './LlmSettings';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const provider = { id:'p1', name:'<img onerror=alert(1)>', provider_type:'local', base_url:'http://127.0.0.1:8080', model:'demo', deployment:null, api_version:null, structured_output:'json_object', timeout_seconds:20, enabled:true, allow_private_network:true, api_key_env:null, api_key:{configured:true,source:'stored'}, last_test_at:null };
describe('LLM settings', () => {
  let container: HTMLDivElement, root: Root;
  beforeEach(() => { container=document.createElement('div'); document.body.append(container); root=createRoot(container); globalThis.fetch=vi.fn(async input => { const path=String(input); if(path.includes('/test?kind=embeddings')) return new Response(JSON.stringify({ok:true,error_category:null,dims:64,latency_ms:12,related_similarity:0.9,unrelated_similarity:0.2,http_status:200})); if(path.endsWith('/test')) return new Response(JSON.stringify({ok:true,expected_answer:false,warning:'Valid but unexpected'})); return new Response(JSON.stringify({items:[provider]})); }) as typeof fetch; });
  afterEach(() => { act(()=>root.unmount()); container.remove(); vi.restoreAllMocks(); });
  async function render() { await act(async()=>{root.render(<LlmSettings/>); await new Promise(r=>setTimeout(r,0));}); }
  it('renders provider strings literally and never echoes secrets into edit inputs', async () => { await render(); expect(container.querySelector('img')).toBeNull(); expect(container.textContent).toContain(provider.name); await act(async()=>container.querySelector<HTMLButtonElement>('.panel-heading .quiet-button')!.click()); const key=container.querySelector<HTMLInputElement>('#provider-key')!; expect(key.type).toBe('password'); expect(key.value).toBe(''); expect(container.textContent).toContain('Credential configured (stored).'); });
  it('runs the unchanged chat check and an embeddings check', async () => {
    await render();
    await act(async()=>{ [...container.querySelectorAll('button')].find(b=>b.textContent==='Test connection')!.click(); await new Promise(r=>setTimeout(r,0)); });
    expect(container.textContent).toContain('Provider returned a valid response, but not the expected answer.');
    expect(container.textContent).toContain('Embeddings check passed: a paraphrase scored higher than an unrelated pair.');
    const calls = vi.mocked(globalThis.fetch).mock.calls.map(([input, init]) => [String(input), init?.method, init?.body]);
    expect(calls).toContainEqual(['/api/v1/admin/llm-providers/p1/test', 'POST', undefined]);
    expect(calls).toContainEqual(['/api/v1/admin/llm-providers/p1/test?kind=embeddings', 'POST', undefined]);
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(2);
  });

  it('shows the embeddings error category and recovery copy', async () => {
    await render();
    globalThis.fetch = vi.fn(async input => String(input).includes('/test?kind=embeddings')
      ? new Response(JSON.stringify({ok:false,error_category:'invalid_response'}))
      : new Response(JSON.stringify({ok:true,expected_answer:true}))) as typeof fetch;
    await act(async()=>{ [...container.querySelectorAll('button')].find(b=>b.textContent==='Test connection')!.click(); await new Promise(r=>setTimeout(r,0)); });
    expect(container.textContent).toContain('Embeddings check failed (invalid_response). Check the model name and that the endpoint supports embeddings.');
  });

  it('shows Azure as unsupported without calling the embeddings test', async () => {
    globalThis.fetch = vi.fn(async input => String(input).endsWith('/admin/llm-providers')
      ? new Response(JSON.stringify({items:[{...provider,provider_type:'azure_openai'}]}))
      : new Response(JSON.stringify({ok:true,expected_answer:true}))) as typeof fetch;
    await render();
    await act(async()=>{ [...container.querySelectorAll('button')].find(b=>b.textContent==='Test connection')!.click(); await new Promise(r=>setTimeout(r,0)); });
    expect(container.textContent).toContain("Embeddings check isn't available for Azure OpenAI.");
    expect(vi.mocked(globalThis.fetch).mock.calls.map(([input]) => String(input)).some(path => path.includes('?kind=embeddings'))).toBe(false);
  });
});
