import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SemanticSettings } from './main';

const project = (semantic: Record<string, unknown> = { method: 'judge', provider_id: 'p1' }) => ({
  id: 'project-1', name: 'Support', slug: 'support', description: null, is_active: true, archived_at: null,
  settings: { corrections: { enabled: false }, deduplication: { mode: 'semantic' as const, semantic } },
  tags: [], created_at: '', updated_at: '',
});
const providers = [
  { id: 'p1', name: 'Local', provider_type: 'local', base_url: 'http://ollama:11434', model: 'judge-model', enabled: true },
  { id: 'azure', name: 'Azure', provider_type: 'azure_openai', base_url: 'https://azure.example', model: null, enabled: true },
];

describe('SemanticSettings embeddings', () => {
  let container: HTMLDivElement; let root: Root; let writes: Record<string, unknown>[];
  beforeEach(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    container = document.createElement('div'); document.body.append(container); root = createRoot(container); writes = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/admin/llm-providers')) return new Response(JSON.stringify({ items: providers }));
      if (url.endsWith('/projects/dedup/defaults')) return new Response(JSON.stringify({ embeddings: { similarity_threshold: 0.73, max_scan: 987, max_scan_cap: 1200 } }));
      if (url.endsWith('/admin/projects/project-1/dedup/preview')) return new Response(JSON.stringify({ method: 'embeddings', verdict: { outcome: 'matched' }, similarity_threshold: 0.73, similarities: Array.from({ length: 7 }, (_, i) => ({ group_id: `g${i + 1}`, representative_event_id: `e${i + 1}`, similarity: 0.9 - i / 100, question_excerpt: `Question ${i + 1}`, threshold_met: i < 3 })) }));
      if (url.endsWith('/projects/project-1') && init?.method === 'PATCH') { writes.push(JSON.parse(String(init.body))); return new Response(JSON.stringify(project((JSON.parse(String(init.body)) as {settings:{deduplication:{semantic:Record<string,unknown>}}}).settings.deduplication.semantic))); }
      return new Response('{}');
    }) as typeof fetch;
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); });
  async function render(saved = project(), onSaved = vi.fn()) {
    await act(async () => { root.render(<SemanticSettings project={saved} onSaved={onSaved} onError={vi.fn()} />); await new Promise(resolve => setTimeout(resolve, 0)); });
  }
  async function selectEmbeddings() {
    await act(async () => { const select = container.querySelector<HTMLSelectElement>('#semantic-method')!; select.value = 'embeddings'; select.dispatchEvent(new Event('change', { bubbles: true })); });
  }

  it('shows the honest embeddings help copy and keeps the judge help unchanged', async () => {
    await render();
    const mode = container.querySelector<HTMLSelectElement>('#dedup-mode')!;
    await act(async () => { mode.value = 'exact'; mode.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(container.querySelector('#dedup-mode-help')?.textContent).toBe('Exact groups identical questions in the same project and event type. To also group paraphrases, use Semantic: the LLM judge method finds the most paraphrases, and the embeddings method groups only very close matches by default.');
    await act(async () => { mode.value = 'semantic'; mode.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(container.querySelector('#semantic-method-help')?.textContent).toBe('Sends each new question and similar existing ones to an LLM, which decides if they match. Slower; handles subtle cases.');
    await selectEmbeddings();
    expect(container.querySelector('#semantic-method-help')?.textContent).toBe('Turns questions into vectors and groups only very close matches. Fast and cheap, and works with a local model such as nomic-embed-text, but by default it misses most paraphrases. To group more paraphrases, use Ask an LLM to judge.');
    expect(container.querySelector('#threshold-help')?.textContent).toBe("Questions at or above this score are grouped. The default is tuned for nomic-embed-text to avoid merging different questions; in our tests it grouped only about 3 in 100 paraphrases. Lowering it groups more paraphrases but merges more different questions: at 0.80 it grouped about 23 in 100 paraphrases, and about 6 in 10 of its groupings were wrong. Other models need their own tuning. Check with the preview, or run dedup-eval on your project's history.");
  });

  it('clears privacy acknowledgement when the method changes and keeps method values', async () => {
    await render();
    const ack = container.querySelector<HTMLInputElement>('#privacy-ack')!;
    await act(async () => { ack.click(); });
    expect(ack.checked).toBe(true);
    await selectEmbeddings();
    expect(container.querySelector<HTMLInputElement>('#privacy-ack')!.checked).toBe(false);
    expect(container.querySelector<HTMLInputElement>('#semantic-model')).toBeNull();
    expect(container.querySelector<HTMLInputElement>('#embedding-model')).not.toBeNull();
    await act(async () => { container.querySelector<HTMLSelectElement>('#semantic-method')!.value = 'judge'; container.querySelector<HTMLSelectElement>('#semantic-method')!.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(container.querySelector<HTMLInputElement>('#semantic-model')!.value).toBe('');
  });

  it('blocks Azure for embeddings and resets the selected Azure provider', async () => {
    await render(project({ method: 'judge', provider_id: 'azure' }));
    await selectEmbeddings();
    expect(container.querySelector<HTMLSelectElement>('#semantic-provider')!.value).toBe('p1');
    expect([...container.querySelectorAll<HTMLOptionElement>('#semantic-provider option')].find(option => option.value === 'azure')?.disabled).toBe(true);
    expect(container.textContent).toContain('Azure not supported');
  });

  it('uses API defaults and rejects threshold and scan values outside their bounds', async () => {
    await render(project({ method: 'embeddings', provider_id: 'p1', embedding_model: 'nomic-embed-text' }));
    expect(container.querySelector<HTMLInputElement>('#similarity-threshold')!.value).toBe('0.73');
    expect(container.querySelector<HTMLInputElement>('#max-scan')!.value).toBe('987');
    await act(async () => {
      const threshold = container.querySelector<HTMLInputElement>('#similarity-threshold')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(threshold, '0.49');
      threshold.dispatchEvent(new Event('input', { bubbles: true }));
      threshold.dispatchEvent(new Event('change', { bubbles: true }));
      container.querySelector<HTMLInputElement>('#privacy-ack')!.click();
      container.querySelector<HTMLFormElement>('.project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('0.50 to 0.99');
    await act(async () => {
      const threshold = container.querySelector<HTMLInputElement>('#similarity-threshold')!;
      const maxScan = container.querySelector<HTMLInputElement>('#max-scan')!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(threshold, '0.73');
      threshold.dispatchEvent(new Event('input', { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(maxScan, '1201');
      maxScan.dispatchEvent(new Event('input', { bubbles: true }));
      container.querySelector<HTMLFormElement>('.project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('1 to 5,000');
    expect(writes).toHaveLength(0);
  });

  it('saves existing judge projects with their judge fields', async () => {
    await render(project({ method: 'judge', provider_id: 'p1', model: 'legacy-model', candidate_count: 4, min_confidence: 0.82, timeout_seconds: 12, max_attempts: 3 }));
    await act(async () => { container.querySelector<HTMLInputElement>('#privacy-ack')!.click(); container.querySelector<HTMLFormElement>('.project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(writes[0].settings).toMatchObject({ deduplication: { mode: 'semantic', privacy_acknowledged: true, semantic: { method: 'judge', provider_id: 'p1', model: 'legacy-model', candidate_count: 4, min_confidence: 0.82, timeout_seconds: 12, max_attempts: 3 } } });
  });

  it('renders embeddings preview as a five-row accessible similarity table', async () => {
    await render(); await selectEmbeddings();
    const form = container.querySelectorAll<HTMLFormElement>('.project-form')[1];
    await act(async () => { form.querySelector('textarea')!.value = 'Password reset?'; form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain('Would group with an existing question.');
    expect(container.querySelectorAll('tbody tr')).toHaveLength(5);
    expect(container.textContent).toContain('Meets threshold');
    expect(container.querySelector('th[scope="col"]')?.textContent).toBe('Similarity');
  });
});
