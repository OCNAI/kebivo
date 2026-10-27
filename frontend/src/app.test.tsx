import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import projectConfig from '../../config/project.json';
import { ApiError, apiRequest, setCsrfToken } from './api';
import { App, DefaultTagsEditor, McpConnectPanel } from './main';
import { Inbox } from './Inbox';

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

describe('frontend toolchain', () => {
  it('reads the display name from shared configuration', () => expect(projectConfig.display_name).toBe('Kebivo'));
});

describe('same-origin API client', () => {
  afterEach(() => { setCsrfToken(null); vi.restoreAllMocks(); });
  it('uses same-origin cookies and keeps CSRF in memory for writes', async () => {
    setCsrfToken('session-csrf');
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }));
    await expect(apiRequest<{ status: string }>('/status', { method: 'POST', body: '{}' })).resolves.toEqual({ status: 'ok' });
    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/status');
    expect(init?.credentials).toBe('same-origin');
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('session-csrf');
  });
  it('surfaces useful API errors without trying to parse HTML', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>not found</html>', { status: 404 }));
    await expect(apiRequest('missing')).rejects.toEqual(new ApiError(404, 'Request failed (404)'));
  });
});

describe('authenticated dashboard', () => {
  let container: HTMLDivElement;
  let root: Root;
  let originalFetch: typeof fetch;
  beforeEach(() => { originalFetch = globalThis.fetch; container = document.createElement('div'); document.body.append(container); root = createRoot(container); });
  afterEach(() => { act(() => root.unmount()); container.remove(); globalThis.fetch = originalFetch; setCsrfToken(null); });

  it('signs in through the API, renders the empty state, and logs out through the API', async () => {
    const calls: { path: string; method: string; headers: Headers }[] = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input); const method = init?.method ?? 'GET'; const headers = new Headers(init?.headers); calls.push({ path, method, headers });
      if (path.endsWith('/auth/session')) return new Response(JSON.stringify({ detail: 'Authentication required' }), { status: 401 });
      if (path.endsWith('/auth/login')) return new Response(JSON.stringify({ admin: { id: 'a1', email: 'admin@example.org' }, csrf_token: 'csrf1' }), { status: 200 });
      if (path.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
      if (path.endsWith('/auth/logout')) return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
      return new Response(JSON.stringify({ detail: 'not found' }), { status: 404 });
    }) as typeof fetch;

    await act(async () => { root.render(<App />); await Promise.resolve(); await Promise.resolve(); });
    expect(container.querySelector('#password')).not.toBeNull();
    await act(async () => {
      const form = container.querySelector<HTMLFormElement>('form')!;
      (container.querySelector<HTMLInputElement>('#email')!).value = 'admin@example.org';
      (container.querySelector<HTMLInputElement>('#password')!).value = 'correct horse battery';
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.querySelector('#dashboard-title')?.textContent).toBe('Dashboard');
    expect(container.textContent).toContain('No active projects');
    expect(container.textContent).not.toContain('Ingestion is not available');
    expect(calls.find((call) => call.path.endsWith('/auth/login'))?.method).toBe('POST');
    expect(calls.find((call) => call.path.endsWith('/projects?limit=100'))?.headers.get('X-CSRF-Token')).toBeNull();

    await act(async () => { container.querySelector<HTMLButtonElement>('.account-trigger')?.click(); });
    await act(async () => { [...container.querySelectorAll<HTMLButtonElement>('.account-menu-panel button')][0]?.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(container.querySelector('#password')).not.toBeNull();
    expect(calls.find((call) => call.path.endsWith('/auth/logout'))?.headers.get('X-CSRF-Token')).toBe('csrf1');
  });

  it('shows the configured origin in a wrong-origin sign-in response', async () => {
    globalThis.fetch = vi.fn(async input => String(input).endsWith('/auth/session')
      ? new Response(JSON.stringify({ detail: 'Authentication required' }), { status: 401 })
      : new Response(JSON.stringify({ detail: 'Page origin is not in ALLOWED_ORIGINS. Configured origins: https://admin.example.org' }), { status: 403 })) as typeof fetch;
    await act(async () => { root.render(<App />); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => { container.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Configured origins: https://admin.example.org');
  });

  it('identifies project correction policy separately from token scope', async () => {
    const project = { id: 'p1', name: 'Support', slug: 'support', description: null, is_active: true, archived_at: null, settings: { corrections: { enabled: false }, deduplication: { mode: 'disabled' as const } }, tags: [], created_at: '', updated_at: '' };
    const writes: Record<string, unknown>[] = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input);
      if (path.endsWith('/projects/p1') && init?.method === 'PATCH') { writes.push(JSON.parse(String(init.body))); return new Response(JSON.stringify({ ...project, settings: JSON.parse(String(init.body)).settings }), { status: 200 }); }
      if (path.endsWith('/auth/session')) return new Response(JSON.stringify({ admin: { id: 'a1', email: 'admin@example.org' }, csrf_token: 'csrf' }), { status: 200 });
      if (path.endsWith('/projects?limit=100')) return new Response(JSON.stringify({ items: [project], next_cursor: null }), { status: 200 });
      if (path.endsWith('/projects/p1')) return new Response(JSON.stringify(project), { status: 200 });
      if (path.endsWith('/tokens')) return new Response(JSON.stringify({ items: [] }), { status: 200 });
      return new Response('{}', { status: 200 });
    }) as typeof fetch;
    await act(async () => { root.render(<App />); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { container.querySelector<HTMLButtonElement>('.project-choice')?.click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    await act(async () => { [...container.querySelectorAll('button')].find(button => button.textContent === 'Edit')?.click(); });
    const mode = container.querySelector<HTMLSelectElement>('#deduplication-mode')!;
    expect(mode.value).toBe('disabled');
    expect([...mode.options].find(option => option.value === 'semantic')?.disabled).toBe(true);
    mode.value = 'exact';
    await act(async () => { mode.dispatchEvent(new Event('change', { bubbles: true })); container.querySelector<HTMLFormElement>('.project-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(writes[0].settings).toMatchObject({ deduplication: { mode: 'exact' } });
    expect(container.textContent).toContain('Corrections policy');
    expect(container.textContent).toContain('feedback:correction');
    expect(container.textContent).toContain('project policy above controls');
  });
  it('keeps focus on an inbox filter when the query string changes', async () => {
    globalThis.fetch=vi.fn(async input=>{const path=String(input);if(path.endsWith('/auth/session'))return new Response(JSON.stringify({admin:{id:'a1',email:'admin@example.org'},csrf_token:'csrf'}));if(path.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[{id:'p1',name:'Support'}]}));if(path.includes('/admin/feedback?'))return new Response(JSON.stringify({items:[],next_cursor:null}));return new Response('{}');}) as typeof fetch;
    history.replaceState({},'', '/');
    await act(async()=>{root.render(<App/>);await new Promise(r=>setTimeout(r,25));});
    await act(async()=>{container.querySelector<HTMLAnchorElement>('.top-nav a[href="/inbox"]')!.click();await new Promise(r=>setTimeout(r,0));});
    const select=container.querySelector<HTMLSelectElement>('select[aria-label="Filter by project"]')!;
    select.focus(); select.value='p1';
    await act(async()=>{select.dispatchEvent(new Event('change',{bubbles:true}));await new Promise(r=>setTimeout(r,0));});
    expect(document.activeElement).toBe(select);
  });
  it('does not move focus when a project selection changes the page heading', async () => {
    const project={id:'p1',name:'Support',slug:'support',description:null,is_active:true,archived_at:null,settings:{corrections:{enabled:false},deduplication:{enabled:false}},tags:[],created_at:'',updated_at:''};
    globalThis.fetch=vi.fn(async input=>{const path=String(input);if(path.endsWith('/auth/session'))return new Response(JSON.stringify({admin:{id:'a1',email:'admin@example.org'},csrf_token:'csrf'}));if(path.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[project]}));if(path.endsWith('/projects/p1'))return new Response(JSON.stringify(project));if(path.endsWith('/projects/p1/tokens'))return new Response(JSON.stringify({items:[]}));return new Response('{}');}) as typeof fetch;
    history.replaceState({},'', '/');
    await act(async()=>{root.render(<App/>);await new Promise(r=>setTimeout(r,0));});
    const choice=container.querySelector<HTMLButtonElement>('.project-choice')!;
    choice.focus();
    await act(async()=>{choice.click();await new Promise(r=>setTimeout(r,0));});
    expect(document.activeElement).toBe(choice);
  });
});

describe('feedback inbox', () => {
  let container: HTMLDivElement; let root: Root; let originalFetch: typeof fetch;
  beforeEach(() => { originalFetch=globalThis.fetch; container=document.createElement('div'); document.body.append(container); root=createRoot(container); });
  afterEach(() => { act(()=>root.unmount()); container.remove(); globalThis.fetch=originalFetch; });
  it('maps filters and sort to query parameters and renders open IT groups', async () => {
    const urls:string[]=[];
    const row={id:'g1',project:{id:'p1',name:'Support',slug:'support'},event_type:'knowledge_gap',representative:{event_id:'e1',question_excerpt:'VPN access issue'},tags:[{slug:'it',name:'IT'}],status:'open',occurrence_count:3,first_seen_at:'2026-01-01T00:00:00Z',last_seen_at:'2026-01-02T00:00:00Z',delivery_summary:{delivered:2,in_progress:0,failed:1}};
    globalThis.fetch=vi.fn(async input=>{const url=String(input);urls.push(url);if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[{id:'p1',name:'Support'}]}));if(url.includes('/admin/groups?'))return new Response(JSON.stringify({items:[row],next_cursor:null}));return new Response('{}');}) as typeof fetch;
    history.replaceState({},'', '/inbox?status=open&tag=it&sort=occurrences_desc&project_id=p1&event_type=knowledge_gap&min_occurrences=2&first_seen_from=2026-01-01T00%3A00%3A00Z');
    await act(async()=>{root.render(<Inbox path="/inbox" navigate={url=>history.pushState({},'',url)}/>);await new Promise(r=>setTimeout(r,0));});
    const request=urls.find(url=>url.includes('/admin/groups?'))!;
    for(const value of ['status=open','tag=it','sort=occurrences_desc','project_id=p1','event_type=knowledge_gap','min_occurrences=2','first_seen_from='])expect(request).toContain(value);
    expect(container.textContent).toContain('VPN access issue'); expect(container.textContent).toContain('IT'); expect(container.textContent).toContain('3');
  });
  it('keeps filters when changed and uses same-origin cookie calls without bearer auth', async () => {
    const requests:{url:string;init?:RequestInit}[]=[];
    globalThis.fetch=vi.fn(async(input,init)=>{const url=String(input);requests.push({url,init});if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[]}));if(url.includes('/admin/groups?'))return new Response(JSON.stringify({items:[],next_cursor:null}));return new Response('{}');}) as typeof fetch;
    history.replaceState({},'', '/inbox?status=open&tag=it');
    await act(async()=>{root.render(<Inbox navigate={url=>history.pushState({},'',url)}/>);await new Promise(r=>setTimeout(r,0));});
    await act(async()=>{container.querySelector<HTMLSelectElement>('[aria-label="Sort groups"]')!.value='occurrences_desc';container.querySelector<HTMLSelectElement>('[aria-label="Sort groups"]')!.dispatchEvent(new Event('change',{bubbles:true}));await new Promise(r=>setTimeout(r,0));});
    expect(window.location.search).toContain('status=open');expect(window.location.search).toContain('tag=it');expect(window.location.search).toContain('sort=occurrences_desc');
    expect(requests.every(({url,init})=>url.startsWith('/api/v1/')&&init?.credentials==='same-origin'&&!new Headers(init?.headers).has('Authorization'))).toBe(true);
  });
  it('appends cursor pages without duplicate groups and shows empty state', async () => {
    let attempts=0;
    const row=(id:string)=>({id,project:{id:'p',name:'P',slug:'p'},event_type:'knowledge_gap',representative:{event_id:id,question_excerpt:id},tags:[],status:'open',occurrence_count:1,first_seen_at:'2026-01-01T00:00:00Z',last_seen_at:'2026-01-01T00:00:00Z',delivery_summary:{delivered:0,in_progress:0,failed:0}});
    globalThis.fetch=vi.fn(async input=>{const url=String(input);if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[]}));if(url.includes('/admin/groups?')){attempts++;if(url.includes('cursor=next'))return new Response(JSON.stringify({items:[row('g1'),row('g2')],next_cursor:null}));if(attempts===1)return new Response(JSON.stringify({items:[row('g1')],next_cursor:'next'}));return new Response(JSON.stringify({items:[],next_cursor:null}));}return new Response('{}');}) as typeof fetch;
    history.replaceState({},'', '/inbox');
    await act(async()=>{root.render(<Inbox navigate={()=>{}}/>);await new Promise(r=>setTimeout(r,0));});
    await act(async()=>{Array.from(container.querySelectorAll<HTMLButtonElement>('button')).find(button=>button.textContent==='Load more')!.click();await new Promise(r=>setTimeout(r,0));});
    expect(container.textContent).toContain('g2');expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
    history.replaceState({},'', '/inbox?tag=missing');
    await act(async()=>{root.render(<Inbox navigate={()=>{}}/>);await new Promise(r=>setTimeout(r,0));});
    expect(container.textContent).toContain('No matching groups');
  });
  it('shows list errors with a working Retry action', async () => {
    let attempts=0;globalThis.fetch=vi.fn(async input=>{const url=String(input);if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[]}));if(url.includes('/admin/groups?')){attempts++;return new Response(attempts===1?'{}':JSON.stringify({items:[],next_cursor:null}),{status:attempts===1?500:200});}return new Response('{}');}) as typeof fetch;
    await act(async()=>{root.render(<Inbox navigate={()=>{}}/>);await new Promise(r=>setTimeout(r,0));});
    expect(container.querySelector('[role="alert"]')).not.toBeNull();await act(async()=>{container.querySelector<HTMLButtonElement>('[role="alert"] button')!.click();await new Promise(r=>setTimeout(r,0));});expect(attempts).toBe(2);
  });
  it('shows MCP configuration with policy-dependent correction access and no real token', () => {
    act(()=>root.render(<McpConnectPanel correctionsEnabled={false}/>));
    expect(container.textContent).toContain(`${window.location.origin}/mcp`); expect(container.textContent).toContain('<PROJECT_TOKEN>'); expect(container.textContent).toContain('feedback:gap'); expect(container.textContent).toContain('feedback:correction'); expect(container.textContent).toContain('disabled'); expect(container.textContent).not.toContain('secret-token');
    const config = container.querySelector('pre[aria-labelledby="mcp-config-heading"]')!.textContent!;
    expect(JSON.parse(config).mcpServers.kebivo.type).toBe('http');
    expect(container.querySelector('label[for="mcp-config"]')).toBeNull();
    act(()=>root.render(<McpConnectPanel correctionsEnabled={true}/>)); expect(container.textContent).toContain('enabled');
  });
  it('copies MCP snippets and announces success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    act(()=>root.render(<McpConnectPanel correctionsEnabled={false}/>));
    await act(async()=>{container.querySelector<HTMLButtonElement>('button')!.click(); await Promise.resolve();});
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('"type": "http"'));
    expect(container.querySelector('[role="status"]')?.textContent).toContain('copied to clipboard');
    await act(async()=>{container.querySelectorAll<HTMLButtonElement>('button')[1].click(); await Promise.resolve();});
    expect(writeText).toHaveBeenLastCalledWith(expect.stringContaining('claude mcp add'));
    expect(container.querySelector('[role="status"]')?.textContent).toContain('Claude Code command copied');
  });
  it('returns to sign-in when a group inbox request gets a 401', async () => {
    history.replaceState({},'', '/');
    globalThis.fetch=vi.fn(async input=>{const url=String(input); if(url.endsWith('/auth/session'))return new Response(JSON.stringify({admin:{id:'a1',email:'admin@example.org'},csrf_token:'csrf'})); if(url.endsWith('/projects?limit=100'))return new Response(JSON.stringify({items:[],next_cursor:null})); if(url.endsWith('/admin/groups/summary'))return new Response(JSON.stringify({total_groups:0,total_events:0,groups_by_status:{},failed_deliveries_last_24h:0,top_open_gaps:[]})); if(url.includes('/admin/groups?'))return new Response(JSON.stringify({detail:'Authentication required'}),{status:401}); return new Response('{}');}) as typeof fetch;
    await act(async()=>{root.render(<App/>); await new Promise(r=>setTimeout(r,0));});
    await act(async()=>{container.querySelector<HTMLAnchorElement>('.top-nav a[href="/inbox"]')!.click(); await new Promise(r=>setTimeout(r,0));});
    expect(container.querySelector('#password')).not.toBeNull();
  });
  it('saves default tags and displays normalized slugs and 422 errors', async () => {
    type Project = Parameters<typeof DefaultTagsEditor>[0]['project'];
    const project:Project={id:'p1',name:'Support',slug:'support',description:null,is_active:true,archived_at:null,settings:{},tags:[{id:'t1',name:'Feature Request',slug:'feature-request',is_default:true}],created_at:'',updated_at:''};
    let fail=false; let saved=0; let error='';
    globalThis.fetch=vi.fn(async(_input,init)=>{if(fail)return new Response(JSON.stringify({detail:'tags[0]: unsupported character'}),{status:422}); const submitted=JSON.parse(String(init?.body)) as {tags:string[]}; return new Response(JSON.stringify({...project,tags:submitted.tags.map((name,i)=>({id:String(i),name,slug:name.toLowerCase().replaceAll(' ','-'),is_default:true}))}));}) as typeof fetch;
    await act(async()=>root.render(<DefaultTagsEditor project={project} busy={false} onSaved={()=>{saved++;}} onError={v=>{error=v;}}/>));
    await act(async()=>{container.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})); await new Promise(r=>setTimeout(r,0));});
    expect(saved).toBe(1); expect(container.textContent).toContain('feature-request');
    fail=true; await act(async()=>{container.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})); await new Promise(r=>setTimeout(r,0));});
    expect(error).toContain('tags[0]: unsupported character');
    expect(container.textContent).not.toContain('Authorization: Bearer');
  });
  it('resets default tags when switching projects and saves to the selected project', async () => {
    type Project = Parameters<typeof DefaultTagsEditor>[0]['project'];
    const projectA:Project={id:'p1',name:'Support',slug:'support',description:null,is_active:true,archived_at:null,settings:{},tags:[{id:'t1',name:'support-only',slug:'support-only',is_default:true}],created_at:'',updated_at:''};
    const projectB:Project={...projectA,id:'p2',name:'Billing',slug:'billing',tags:[{id:'t2',name:'billing-only',slug:'billing-only',is_default:true}]};
    const requests:{url:string;body:unknown}[]=[];
    globalThis.fetch=vi.fn(async(input,init)=>{requests.push({url:String(input),body:JSON.parse(String(init?.body))});return new Response(JSON.stringify(projectB));}) as typeof fetch;
    const renderProject=(project:Project)=><DefaultTagsEditor key={project.id} project={project} busy={false} onSaved={()=>{}} onError={()=>{}}/>;
    await act(async()=>root.render(renderProject(projectA)));
    expect(container.querySelector<HTMLTextAreaElement>('#default-tags')?.value).toBe('support-only');
    await act(async()=>root.render(renderProject(projectB)));
    expect(container.querySelector<HTMLTextAreaElement>('#default-tags')?.value).toBe('billing-only');
    await act(async()=>{container.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await new Promise(r=>setTimeout(r,0));});
    expect(requests[0].url).toBe('/api/v1/projects/p2/default-tags');
    expect(requests[0].body).toEqual({tags:['billing-only']});
  });
});

