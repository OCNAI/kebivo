import { afterEach,describe,expect,it,vi } from 'vitest';
import { act } from 'react';
import { createRoot,type Root } from 'react-dom/client';
import { ConnectPanel } from './ConnectPanel';
import { GeneralSettings } from './Settings';

Object.assign(globalThis,{IS_REACT_ACT_ENVIRONMENT:true});
describe('P7 settings and integration',()=>{
  let host:HTMLDivElement; let root:Root; const originalFetch=globalThis.fetch;
  afterEach(()=>{act(()=>root.unmount());host.remove();globalThis.fetch=originalFetch;vi.restoreAllMocks();});
  it('shows read-only retention settings, disabled classes, runs, operations and an export link',async()=>{
    host=document.createElement('div');document.body.append(host);root=createRoot(host);
    globalThis.fetch=vi.fn(async input=>{const path=String(input);if(path.endsWith('/admin/settings'))return new Response(JSON.stringify({version:'0.1.0',retention:{feedback_days:0,delivery_log_days:30,audit_days:90,next_run_at:'2026-10-22T02:10:00Z',last_runs:[{started_at:'2026-10-21T02:10:00Z',finished_at:'2026-10-21T02:11:00Z',status:'failed',counts:{feedback:0,delivery_attempts:4,audit_events:2},error_category:'database_busy'}]},operations:{mcp_enabled:true,feedback_rate_limit_per_token:120,feedback_rate_limit_per_ip:300,body_limit_bytes:65536,private_webhook_default:false,trusted_proxies_count:0}}));return new Response(JSON.stringify({items:[{id:'p1',name:'Support',slug:'support',archived_at:null}]}));}) as typeof fetch;
    await act(async()=>{root.render(<GeneralSettings navigate={()=>{}}/>);await new Promise(r=>setTimeout(r,25));});
    expect(host.textContent).toContain('Automatic deletion is off');expect(host.textContent).toContain('4 removed');expect(host.textContent).toContain('Cause: database_busy');
    expect(host.querySelector('a[download]')?.getAttribute('href')).toBe('/api/v1/admin/projects/p1/export');expect(host.querySelectorAll('input,button[type="submit"]')).toHaveLength(0);
  });
  it('keeps user-entered project values out of snippets and never inserts a real token',()=>{
    host=document.createElement('div');document.body.append(host);root=createRoot(host);Object.defineProperty(window,'location',{configurable:true,value:{origin:'https://kebivo.example.org'}});
    act(()=>root.render(<ConnectPanel correctionsEnabled={false}/>));
    expect(host.textContent).not.toContain('Bad\n"name');expect(host.textContent).toContain('Bearer $KEBIVO_TOKEN');expect(host.textContent).toContain('<PROJECT_TOKEN>');expect(host.textContent).not.toContain('kebivo_pt_');
  });
  it('uses a safe local origin when the browser origin contains whitespace or quotes',()=>{
    host=document.createElement('div');document.body.append(host);root=createRoot(host);Object.defineProperty(window,'location',{configurable:true,value:{origin:"https://bad' origin"}});
    act(()=>root.render(<ConnectPanel correctionsEnabled/>));expect(host.textContent).toContain('http://127.0.0.1:8000/api/v1/feedback');
  });
  it('sends a test event and links to its group',async()=>{
    host=document.createElement('div');document.body.append(host);root=createRoot(host);
    const fetch=vi.fn(async()=>new Response(JSON.stringify({event_id:'e1',group_id:'g1'}),{status:202}));globalThis.fetch=fetch as typeof globalThis.fetch;
    await act(async()=>root.render(<ConnectPanel correctionsEnabled projectId="p1"/>));
    expect(host.textContent).toContain('Sent like a real report. Matching routing rules deliver it to their destinations.');
    await act(async()=>{host.querySelector<HTMLButtonElement>('button')!.click();await new Promise(resolve=>setTimeout(resolve,0));});
    expect(fetch).toHaveBeenCalledWith('/api/v1/projects/p1/test-event',expect.objectContaining({method:'POST'}));
    expect(host.textContent).toContain('Test event sent.');
    expect(host.querySelector<HTMLAnchorElement>('a[href^="/inbox/groups/g1"]')?.textContent).toBe('View group');
  });
  it('disables test events for archived projects and reports endpoint errors',async()=>{
    host=document.createElement('div');document.body.append(host);root=createRoot(host);
    const fetch=vi.fn(async()=>new Response(JSON.stringify({detail:'Project is archived.'}),{status:409}));globalThis.fetch=fetch as typeof globalThis.fetch;
    await act(async()=>root.render(<ConnectPanel correctionsEnabled projectId="p1" archived/>));
    const button=host.querySelector<HTMLButtonElement>('button')!;expect(button.disabled).toBe(true);expect(fetch).not.toHaveBeenCalled();
    act(()=>root.render(<ConnectPanel correctionsEnabled projectId="p1"/>));
    await act(async()=>{host.querySelector<HTMLButtonElement>('button')!.click();await new Promise(resolve=>setTimeout(resolve,0));});
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Project is archived.');
  });
});
