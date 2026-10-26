import React from 'react';
import { createRoot } from 'react-dom/client';

import projectConfig from '../../config/project.json';
import { ApiError, apiRequest, setCsrfToken } from './api';
import './style.css';
import { Inbox } from './Inbox';
import { Destinations } from './Destinations';
import { Routing } from './Routing';
import { Deliveries } from './Deliveries';
import { DashboardSummary, FirstRunChecklist } from './DashboardSummary';
import { GroupDetail } from './GroupDetail';
import { LlmSettings } from './LlmSettings';
import { GeneralSettings, SettingsNav } from './Settings';
import { ConnectPanel } from './ConnectPanel';

document.title = projectConfig.display_name;

type Admin = { id: string; email: string };
type Project = {
  id: string; name: string; slug: string; description: string | null; is_active: boolean;
  archived_at: string | null; settings: { corrections?: { enabled?: boolean }; deduplication?: { mode?: 'disabled' | 'exact' | 'semantic'; enabled?: boolean } };
  tags: { id: string; name: string; slug: string; is_default: boolean }[]; created_at: string; updated_at: string;
};
type Token = { id: string; name: string; prefix: string; scopes: string[]; created_at: string; expires_at: string | null; last_used_at: string | null; revoked_at: string | null };
type CreatedToken = Token & { token: string };
const date = (value: string | null) => value ? new Date(value).toLocaleDateString() : 'Never';

function apiErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) return 'Your session has expired. Sign in again to continue.';
  if (error instanceof ApiError && error.status === 429) return 'Too many sign-in attempts. Wait a while before trying again.';
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.';
}

function LegacyEventLink({ eventId, navigate }: { eventId: string; navigate: (url: string) => void }) {
  React.useEffect(() => { let active = true; apiRequest<{ group: { id: string } | null }>(`admin/feedback/${encodeURIComponent(eventId)}`).then(event => { if (active && event.group) navigate(`/inbox/groups/${event.group.id}?highlight=${encodeURIComponent(eventId)}`); }).catch(() => { if (active) navigate('/inbox'); }); return () => { active = false; }; }, [eventId, navigate]);
  return <main className="center"><p role="status">Opening feedback group…</p></main>;
}

export function App() {
  const [route, setRoute] = React.useState(location.pathname + location.search);
  const pathname = route.split('?')[0];
  const navigate = React.useCallback((url: string) => { history.pushState({}, '', url); setRoute(location.pathname + location.search); window.dispatchEvent(new PopStateEvent('popstate')); }, []);
  const [admin, setAdmin] = React.useState<Admin | null>(null);
  const [authReady, setAuthReady] = React.useState(false);
  const [projects, setProjects] = React.useState<Project[]>([]);
  const [selected, setSelected] = React.useState<Project | null>(null);
  const [tokens, setTokens] = React.useState<Token[]>([]);
  const [createdToken, setCreatedToken] = React.useState<CreatedToken | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const [notice, setNotice] = React.useState('');
  const [showArchived, setShowArchived] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  React.useEffect(() => { const pop = () => setRoute(location.pathname + location.search); const expired = () => { setAdmin(null); setSelected(null); setCsrfToken(null); }; addEventListener('popstate', pop); addEventListener('session-expired', expired); return () => { removeEventListener('popstate', pop); removeEventListener('session-expired', expired); }; }, []);
  React.useEffect(() => {
  const page = route.startsWith('/inbox') ? 'Inbox' : route.startsWith('/settings') ? 'Settings' : route.startsWith('/deliveries') ? 'Deliveries' : route.startsWith('/destinations') ? 'Destinations' : selected?.name ?? 'Projects';
    document.title = `${page} · ${projectConfig.display_name}`;
  }, [route, selected?.name]);
  React.useLayoutEffect(() => {
    document.querySelector<HTMLElement>('main h1')?.focus({ preventScroll: true });
  }, [pathname, Boolean(admin)]);

  const refreshProjects = React.useCallback(async (includeArchived = showArchived) => {
    const result = await apiRequest<{ items: Project[]; next_cursor: string | null }>(`projects?limit=100${includeArchived ? '&include_archived=true' : ''}`);
    setProjects(result.items);
  }, [showArchived]);
  const selectProject = React.useCallback(async (project: Project) => {
    setError(''); setNotice(''); setCreatedToken(null); setEditing(false);
    setSelected(project);
    try {
      const [fresh, tokenResult] = await Promise.all([
        apiRequest<Project>(`projects/${project.id}`),
        apiRequest<{ items: Token[] }>(`projects/${project.id}/tokens`),
      ]);
      setSelected(fresh); setTokens(tokenResult.items);
      if (location.hash === '#routing') requestAnimationFrame(() => document.getElementById('routing')?.scrollIntoView({ block: 'start' }));
    } catch (e) { setError(apiErrorMessage(e)); }
  }, [setError, setNotice, setCreatedToken, setEditing, setSelected, setTokens]);

  React.useEffect(() => {
    const projectId = new URLSearchParams(location.search).get('project');
    const project = projectId ? projects.find((item) => item.id === projectId) : undefined;
    if (project && selected?.id !== project.id) queueMicrotask(() => void selectProject(project));
  }, [projects, selectProject, selected?.id]);

  React.useEffect(() => {
    let live = true;
    apiRequest<{ admin: Admin; csrf_token: string }>('auth/session').then((session) => {
      if (!live) return;
      setCsrfToken(session.csrf_token); setAdmin(session.admin);
      return refreshProjects(false);
    }).catch((e: unknown) => {
      if (e instanceof ApiError && e.status !== 401 && live) setError(apiErrorMessage(e));
    }).finally(() => { if (live) setAuthReady(true); });
    return () => { live = false; };
  }, [refreshProjects]);

  async function run<T>(action: () => Promise<T>, success?: (result: T) => void) {
    setBusy(true); setError(''); setNotice('');
    try { const result = await action(); success?.(result); }
    catch (e) {
      setError(apiErrorMessage(e));
      if (e instanceof ApiError && e.status === 401) { setAdmin(null); setSelected(null); setCsrfToken(null); }
    } finally { setBusy(false); }
  }

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    await run(async () => apiRequest<{ admin: Admin; csrf_token: string }>('auth/login', {
      method: 'POST', body: JSON.stringify({ email: data.get('email'), password: data.get('password') }),
    }), async (session) => { setCsrfToken(session.csrf_token); setAdmin(session.admin); await refreshProjects(false); });
  }

  async function logout() {
    await run(() => apiRequest<{ status: string }>('auth/logout', { method: 'POST', body: '{}' }), () => {
      setCsrfToken(null); setAdmin(null); setSelected(null); setProjects([]); setTokens([]); setCreatedToken(null);
    });
  }

  async function saveProject(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    const settings = { ...selected?.settings, corrections: { enabled: data.get('corrections') === 'on' }, deduplication: { mode: String(data.get('deduplication_mode')) as 'disabled' | 'exact' | 'semantic' } };
    const payload = { name: String(data.get('name')), slug: String(data.get('slug')), description: String(data.get('description')) || null, settings, ...(selected ? { is_active: data.get('is_active') === 'on' } : {}) };
    await run(async () => selected
      ? apiRequest<Project>(`projects/${selected.id}`, { method: 'PATCH', body: JSON.stringify(payload) })
      : apiRequest<Project>('projects', { method: 'POST', body: JSON.stringify(payload) }), async (project) => {
      setSelected(project); setEditing(false); setNotice(selected ? 'Project settings saved.' : 'Project created.');
      await refreshProjects();
      const tokenResult = await apiRequest<{ items: Token[] }>(`projects/${project.id}/tokens`); setTokens(tokenResult.items);
    });
  }

  async function changeProjectState(project: Project, action: 'archive' | 'restore') {
    await run(() => apiRequest<Project>(`projects/${project.id}/${action}`, { method: 'POST', body: '{}' }), async (updated) => {
      setSelected(updated); setNotice(action === 'archive' ? 'Project archived.' : 'Project restored.'); await refreshProjects();
    });
  }

  async function createToken(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selected) return;
    const data = new FormData(event.currentTarget);
    const scopes = ['feedback:gap', ...(data.get('correction-scope') === 'on' ? ['feedback:correction'] : [])];
    const form = event.currentTarget;
    await run(() => apiRequest<CreatedToken>(`projects/${selected.id}/tokens`, { method: 'POST', body: JSON.stringify({ name: data.get('token-name'), scopes }) }), async (token) => {
      setCreatedToken(token); setNotice('Token created. Copy it now; it cannot be shown again.');
      const result = await apiRequest<{ items: Token[] }>(`projects/${selected.id}/tokens`); setTokens(result.items);
      form.reset();
    });
  }

  async function revokeToken(token: Token) {
    if (!selected || !window.confirm(`Revoke “${token.name}”? Applications using it will stop authenticating.`)) return;
    await run(() => apiRequest<Token>(`projects/${selected.id}/tokens/${token.id}/revoke`, { method: 'POST', body: '{}' }), async () => {
      setNotice('Token revoked.'); const result = await apiRequest<{ items: Token[] }>(`projects/${selected.id}/tokens`); setTokens(result.items);
    });
  }

  if (!authReady) return <Shell><main className="center"><p role="status">Checking your session…</p></main></Shell>;
  if (!admin) return <Shell><main className="center"><section className="welcome-card" aria-labelledby="welcome-title">
    <div className="eyebrow">ADMINISTRATOR ACCESS</div><h1 id="welcome-title" tabIndex={-1}>Sign in to your workspace</h1>
    <p className="intro">Use the administrator account created from the server command line.</p>
    <ErrorMessage message={error} />
    <form onSubmit={login}>
      <label htmlFor="email">Email address</label><input id="email" name="email" type="email" autoComplete="username" required />
      <label htmlFor="password">Password</label><input id="password" name="password" type="password" autoComplete="current-password" required />
      <button disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
    </form>
    <p className="fine-print">No signup. Administrators are created from the server command line. See the <a className="app-link" href="/docs">server documentation</a>.</p>
  </section></main></Shell>;

  if (route.startsWith('/inbox/groups/')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><GroupDetail groupId={location.pathname.split('/')[3] ?? ''} navigate={navigate} /></Shell>;
  if (route.startsWith('/settings/llm')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><main className="dashboard-main"><section className="dashboard"><div className="dashboard-heading"><div><div className="eyebrow">CONFIGURATION</div><h1 tabIndex={-1}>Settings</h1><p className="subtle">Configure optional language model providers.</p></div></div><SettingsNav active="llm" navigate={navigate}/><LlmSettings /></section></main></Shell>;
  if (route.startsWith('/settings')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><GeneralSettings navigate={navigate}/></Shell>;
  if (/^\/inbox\/[^/]+$/.test(location.pathname)) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><LegacyEventLink eventId={location.pathname.split('/')[2]} navigate={navigate} /></Shell>;
  if (route.startsWith('/inbox')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><Inbox path={location.pathname} navigate={navigate} /></Shell>;
  if (route.startsWith('/deliveries')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><Deliveries navigate={navigate} /></Shell>;
  if (route.startsWith('/destinations')) return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><Destinations /></Shell>;
  return <Shell navigate={navigate} route={route} accountEmail={admin.email} onSignOut={logout}><main className="dashboard-main">
    <section className="dashboard" aria-labelledby="dashboard-title">
      <div className="dashboard-heading"><div><div className="eyebrow">KEBIVO</div><h1 id="dashboard-title" tabIndex={-1}>Dashboard</h1><p className="subtle">Feedback activity and project settings.</p></div></div>
      <ErrorMessage message={error} /><div role="status" className="notice">{notice}</div>
      {!selected && !editing && <FirstRunChecklist projects={projects.filter(project=>!project.archived_at)} navigate={navigate} />}
      {!selected && !editing && <DashboardSummary navigate={navigate} projectId={projects.find(project=>!project.archived_at)?.id} />}
      <div className="dashboard-grid">
        <section className="panel project-panel" aria-labelledby="projects-title">
          <div className="panel-heading"><div><h2 id="projects-title">Projects</h2><p className="subtle">{projects.length} {projects.length === 1 ? 'project' : 'projects'}</p></div><button onClick={() => { setSelected(null); setEditing(true); setError(''); setNotice(''); }}>New project</button></div>
          {projects.filter((project) => showArchived || !project.archived_at).length === 0 ? <div className="empty-state"><h3>{showArchived ? 'No projects yet' : 'No active projects'}</h3><p>Create a project to organize feedback and issue credentials.</p><button onClick={() => { setSelected(null); setEditing(true); }}>Create a project</button></div> : <ul className="project-list">{projects.filter((project) => showArchived || !project.archived_at).map((project) => <li key={project.id}>
            <button className={`project-choice ${selected?.id === project.id ? 'selected' : ''}`} onClick={() => void selectProject(project)} aria-current={selected?.id === project.id ? 'true' : undefined}>
              <span className="project-avatar" aria-hidden="true">{project.name.slice(0, 1).toUpperCase()}</span><span className="project-info"><strong>{project.name}</strong><small>{project.slug}</small></span><span className={`badge ${project.is_active ? 'active' : ''}`}>{project.is_active ? 'Active' : 'Disabled'}</span>
            </button></li>)}</ul>}
          <label className="checkbox-row archived-toggle"><input type="checkbox" checked={showArchived} onChange={(e) => { setShowArchived(e.target.checked); void refreshProjects(e.target.checked).catch((err: unknown) => setError(apiErrorMessage(err))); }} /> Show archived projects</label>
          {showArchived && <ArchivedProjects projects={projects} onRestore={(p) => void changeProjectState(p, 'restore')} />}
        </section>
        <section className="detail-column" aria-live="polite">
          {editing ? <ProjectForm project={selected} busy={busy} onCancel={() => setEditing(false)} onSubmit={saveProject} /> : selected ? <>
            <section className="panel detail-panel" aria-labelledby="project-detail-title">
              <div className="panel-heading"><div><div className="eyebrow">PROJECT SETTINGS</div><h2 id="project-detail-title">{selected.name}</h2><p className="subtle">{selected.description || 'No description'}</p></div><div className="button-row"><a className="text-button" href={`/settings?project=${encodeURIComponent(selected.id)}#export`} onClick={event=>{event.preventDefault();navigate(`/settings?project=${encodeURIComponent(selected.id)}#export`)}}>Export feedback</a><button className="quiet-button" onClick={() => setEditing(true)}>Edit</button>{selected.archived_at ? <button className="quiet-button" onClick={() => void changeProjectState(selected, 'restore')}>Restore</button> : <button className="danger-button" onClick={() => window.confirm(`Archive “${selected.name}”? This disables its tokens but keeps settings and history.`) && void changeProjectState(selected, 'archive')}>Archive</button>}</div></div>
              <dl className="settings-summary"><div><dt>Slug</dt><dd>{selected.slug}</dd></div><div><dt>Status</dt><dd>{selected.archived_at ? 'Archived' : selected.is_active ? 'Active' : 'Disabled'}</dd></div><div><dt>Corrections policy</dt><dd>{selected.settings.corrections?.enabled ? 'Enabled' : 'Disabled'}</dd></div><div><dt>Deduplication</dt><dd>{selected.settings.deduplication?.mode ?? (selected.settings.deduplication?.enabled === false ? 'Disabled' : 'Exact')}</dd></div></dl>
            </section>
            <section className="panel" aria-labelledby="tokens-title"><div className="panel-heading"><div><div className="eyebrow">CREDENTIALS</div><h2 id="tokens-title">Project tokens</h2></div></div>
              {createdToken && <div className="secret-reveal" role="alert"><strong>Copy this token now</strong><p>This secret is shown once. Store it securely; it cannot be retrieved later.</p><label htmlFor="new-token">New token</label><div className="copy-row"><input id="new-token" readOnly value={createdToken.token} onFocus={(e) => e.currentTarget.select()} /><button type="button" className="quiet-button" onClick={() => void navigator.clipboard?.writeText(createdToken.token).then(() => setNotice('Token copied to clipboard.')).catch(() => setNotice('Select and copy the token manually.'))}>Copy token</button></div><button className="quiet-button dismiss-button" onClick={() => setCreatedToken(null)}>I have saved it</button></div>}
              {!selected.archived_at && <form className="token-form" onSubmit={createToken}><div className="form-row"><div><label htmlFor="token-name">Token name</label><input id="token-name" name="token-name" maxLength={120} placeholder="Production integration" required /></div><button disabled={busy}>Create token</button></div><p className="subtle">All tokens receive <code>feedback:gap</code>.</p><label className="checkbox-row"><input name="correction-scope" type="checkbox" disabled={!selected.settings.corrections?.enabled} /> Also grant <code>feedback:correction</code>{!selected.settings.corrections?.enabled && <span className="subtle"> (enable project correction policy first)</span>}</label></form>}
              {tokens.length === 0 ? <div className="empty-inline">No tokens have been issued for this project.</div> : <div className="table-wrap"><table><thead><tr><th scope="col">Name</th><th scope="col">Scopes</th><th scope="col">Created</th><th scope="col">Last used</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead><tbody>{tokens.map((token) => <tr key={token.id} className={token.revoked_at ? 'revoked-row' : ''}><th scope="row">{token.name}<small>{token.prefix}… {token.revoked_at ? ' · Revoked' : ''}</small></th><td>{token.scopes.map((scope) => <code className="scope-chip" key={scope}>{scope}</code>)}</td><td>{date(token.created_at)}</td><td>{date(token.last_used_at)}</td><td>{!token.revoked_at && <button className="text-button" disabled={busy || Boolean(selected.archived_at)} onClick={() => void revokeToken(token)}>Revoke</button>}</td></tr>)}</tbody></table></div>}
            </section>
              <ConnectPanel correctionsEnabled={Boolean(selected.settings.corrections?.enabled)} archived={Boolean(selected.archived_at)} projectId={selected.id} />
            <section className="panel project-configuration" aria-label="Project configuration">
      <DefaultTagsEditor key={selected.id} project={selected} busy={busy} onSaved={(updated) => { setSelected(updated); setNotice('Default tags saved.'); }} onError={setError} />
              <SemanticSettings key={`semantic-${selected.id}`} project={selected} onSaved={updated => { setSelected(updated); setNotice('Deduplication settings saved.'); }} onError={setError} />
              <div id="routing"><Routing project={selected} /></div>
              <p className="scope-note"><strong>Correction access uses two gates.</strong> The project policy above controls whether corrections are accepted. Each token must also include the <code>feedback:correction</code> scope.</p>
              {selected.archived_at && <p className="inline-note">Archived {date(selected.archived_at)}. Restore this project before issuing credentials.</p>}
            </section>
          </> : <div className="empty-card"><div className="empty-icon" aria-hidden="true">✦</div><h2>Select a project</h2><p>Choose a project to manage its settings and credentials, or create a new one.</p></div>}
        </section>
      </div>
    </section>
  </main></Shell>;
}

function Shell({ children, navigate, route, accountEmail, onSignOut }: { children: React.ReactNode; navigate?: (url: string) => void; route?: string; accountEmail?: string; onSignOut?: () => void }) { const link = (url: string) => (event: React.MouseEvent) => { event.preventDefault(); navigate?.(url); }; return <div className="app-shell"><header className="topbar"><a className="brand" href="/" onClick={link('/')} aria-label={`${projectConfig.display_name} home`}><BrandMark/><span>{projectConfig.display_name}</span></a>{navigate && <nav aria-label="Main navigation" className="top-nav"><a href="/" onClick={link('/')} aria-current={route === '/' ? 'page' : undefined}>Dashboard</a><a href="/inbox" onClick={link('/inbox')} aria-current={route?.startsWith('/inbox') ? 'page' : undefined}>Inbox</a><a href="/destinations" onClick={link('/destinations')} aria-current={route?.startsWith('/destinations') ? 'page' : undefined}>Destinations</a><a href="/deliveries" onClick={link('/deliveries')} aria-current={route?.startsWith('/deliveries') ? 'page' : undefined}>Deliveries</a><a href="/settings" onClick={link('/settings')} aria-current={route?.startsWith('/settings') ? 'page' : undefined}>Settings</a></nav>}{accountEmail&&<AccountMenu email={accountEmail} onSignOut={onSignOut??(()=>{})}/>}</header>{children}<footer>{projectConfig.display_name} · self-hosted</footer></div>; }
function AccountMenu({email,onSignOut}:{email:string;onSignOut:()=>void}){const [open,setOpen]=React.useState(false);const root=React.useRef<HTMLDivElement>(null);const trigger=React.useRef<HTMLButtonElement>(null);return <div className="account-menu" ref={root} onBlur={event=>{if(!root.current?.contains(event.relatedTarget as Node|null))setOpen(false)}} onKeyDown={event=>{if(event.key==='Escape'&&open){setOpen(false);trigger.current?.focus()}}}><button ref={trigger} type="button" className="account-trigger" aria-expanded={open} onClick={()=>setOpen(value=>!value)}><span className="account-email">{email}</span><span className="account-mobile-label">Account</span><span aria-hidden="true">▾</span></button>{open&&<div className="account-menu-panel"><button type="button" className="quiet-button" onClick={onSignOut}>Sign out</button></div>}</div>;}
function BrandMark(){return <svg className="brand-mark" aria-hidden="true" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#35735e"/><g fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="M11 8v16"/><path d="m11.5 16.5 9.5-8"/><path d="m15.5 13.1 6 10.9"/></g></svg>;}
function ErrorMessage({ message }: { message: string }) { return message ? <p className="error-message" role="alert">{message}</p> : null; }
export function DefaultTagsEditor({ project, busy, onSaved, onError }: { project: Project; busy: boolean; onSaved: (project: Project) => void; onError: (message: string) => void }) {
  const [value, setValue] = React.useState(project.tags.filter(tag=>tag.is_default).map(tag=>tag.name).join('\n'));
  const [saving, setSaving] = React.useState(false);
  const defaults = project.tags.filter(tag=>tag.is_default);
  async function submit(event: React.FormEvent) { event.preventDefault(); onError(''); setSaving(true); try { const result = await apiRequest<Project>(`projects/${project.id}/default-tags`, { method:'PUT', body:JSON.stringify({tags:value.split(/[\n,]/).map(v=>v.trim()).filter(Boolean)}) }); onSaved(result); setValue(result.tags.filter(tag=>tag.is_default).map(tag=>tag.name).join('\n')); } catch(e) { onError(apiErrorMessage(e)); } finally { setSaving(false); } }
  return <section className="default-tags" aria-labelledby="default-tags-title"><h3 id="default-tags-title">Default tags</h3><p className="subtle">One tag per line. Submitted tags stay visible on existing feedback.</p><form onSubmit={submit}><label htmlFor="default-tags">Default tags</label><textarea id="default-tags" rows={4} value={value} onChange={e=>setValue(e.target.value)} disabled={busy||saving||Boolean(project.archived_at)} /><button disabled={busy||saving||Boolean(project.archived_at)}>{saving?'Saving…':'Save default tags'}</button></form><div className="normalized-tags"><strong>Normalized slugs</strong>{defaults.length ? <ul>{defaults.map(tag=><li key={tag.slug}><span>{tag.name}</span><code>{tag.slug}</code></li>)}</ul> : <p className="subtle">No default tags.</p>}</div></section>;
}
function SemanticSettings({ project, onSaved, onError }: { project: Project; onSaved: (project: Project) => void; onError: (message: string) => void }) {
  type Provider = { id: string; name: string; model: string | null; enabled: boolean };
  const d = project.settings.deduplication ?? {}, sem = (d as typeof d & { semantic?: { provider_id?: string; model?: string; candidate_count?: number; min_confidence?: number; timeout_seconds?: number; max_attempts?: number } }).semantic;
  const [providers, setProviders] = React.useState<Provider[]>([]), [preview, setPreview] = React.useState<Record<string, unknown> | null>(null), [saving, setSaving] = React.useState(false);
  const [mode, setMode] = React.useState<string>(d.mode ?? (d.enabled === false ? 'disabled' : 'exact'));
  const [ack, setAck] = React.useState(false);
  React.useEffect(() => { apiRequest<{items:Provider[]}>('admin/llm-providers').then(r => setProviders(r.items.filter(p => p.enabled))).catch(e => onError(e.message)); }, [onError]);
  async function submit(e: React.FormEvent<HTMLFormElement>) { e.preventDefault(); const form = new FormData(e.currentTarget); if (mode === 'semantic' && !ack) { onError('Acknowledge the privacy disclosure before saving semantic matching.'); return; }
    const deduplication = mode === 'semantic' ? { mode, privacy_acknowledged: true, semantic: { provider_id: String(form.get('provider_id')), model: String(form.get('semantic_model') || '') || null, candidate_count: Number(form.get('candidate_count')), min_confidence: Number(form.get('min_confidence')), timeout_seconds: Number(form.get('semantic_timeout')), max_attempts: Number(form.get('max_attempts')) } } : { mode };
    setSaving(true); try { const updated = await apiRequest<Project>(`projects/${project.id}`, { method:'PATCH', body: JSON.stringify({ settings: { ...project.settings, deduplication } }) }); onSaved(updated); } catch(e) { onError(apiErrorMessage(e)); } finally { setSaving(false); }
  }
  async function runPreview(e: React.FormEvent<HTMLFormElement>) { e.preventDefault(); const form = new FormData(e.currentTarget); setPreview(null); try { setPreview(await apiRequest(`admin/projects/${project.id}/dedup/preview`, { method:'POST', body: JSON.stringify({event_type: form.get('event_type'), question: form.get('question')}) })); } catch(e) { onError(apiErrorMessage(e)); } }
  return <section className="panel" aria-labelledby="semantic-title"><h2 id="semantic-title">Deduplication and semantic matching</h2><form className="project-form" onSubmit={e => void submit(e)}><label htmlFor="dedup-mode">Mode</label><select id="dedup-mode" value={mode} onChange={e => setMode(e.target.value)}><option value="disabled">Disabled</option><option value="exact">Exact</option><option value="semantic" disabled={!providers.length}>Semantic{!providers.length ? ' (configure an enabled provider first)' : ''}</option></select><p className="help-text">Semantic matching sends submitted question text and up to 10 similar question excerpts to the selected provider. It does not send names, metadata, or group IDs. Provider retention and processing policies apply.</p>{mode === 'semantic' && <><label htmlFor="semantic-provider">Provider</label><select id="semantic-provider" name="provider_id" defaultValue={sem?.provider_id ?? providers[0]?.id} required>{providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select><label htmlFor="semantic-model">Model override (optional)</label><input id="semantic-model" name="semantic_model" defaultValue={sem?.model ?? ''}/><label htmlFor="candidate-count">Candidate groups (1–10)</label><input id="candidate-count" name="candidate_count" type="number" min="1" max="10" defaultValue={sem?.candidate_count ?? 5}/><label htmlFor="min-confidence">Minimum confidence</label><input id="min-confidence" name="min_confidence" type="number" min="0.5" max="1" step="0.01" defaultValue={sem?.min_confidence ?? 0.85}/><label htmlFor="semantic-timeout">Timeout (seconds)</label><input id="semantic-timeout" name="semantic_timeout" type="number" min="1" max="30" defaultValue={sem?.timeout_seconds ?? 15}/><label htmlFor="max-attempts">Maximum attempts</label><input id="max-attempts" name="max_attempts" type="number" min="1" max="3" defaultValue={sem?.max_attempts ?? 2}/><label className="checkbox-row"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)}/> I understand question text will be sent to the selected provider for matching.</label></>}<button disabled={saving}>{saving ? 'Saving…' : 'Save deduplication settings'}</button></form><hr/><h3>Preview a submission</h3><form className="project-form" onSubmit={e => void runPreview(e)}><label htmlFor="preview-type">Event type</label><select id="preview-type" name="event_type"><option value="knowledge_gap">Knowledge gap</option><option value="knowledge_correction">Knowledge correction</option></select><label htmlFor="preview-question">Question</label><textarea id="preview-question" name="question" required maxLength={10000}/><button>Run preview</button></form>{preview && <pre className="safe-pre" role="status">{JSON.stringify(preview,null,2)}</pre>}</section>;
}
export function McpConnectPanel({ correctionsEnabled }: { correctionsEnabled: boolean }) {
  const endpoint = `${window.location.origin}/mcp`;
  const config = JSON.stringify({ mcpServers: { kebivo: { type: 'http', url: endpoint, headers: { Authorization: 'Bearer <PROJECT_TOKEN>' } } } }, null, 2);
  const claude = `claude mcp add --transport http kebivo ${endpoint} --header "Authorization: Bearer <PROJECT_TOKEN>"`;
  const [notice, setNotice] = React.useState('');
  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setNotice(`${label} copied to clipboard.`); }
    catch { setNotice(`Could not copy ${label.toLowerCase()}. Select the text and copy it manually.`); }
  }
  return <section className="mcp-panel" aria-labelledby="mcp-connect-title"><h3 id="mcp-connect-title">Connect over MCP</h3><p className="subtle">Endpoint: <code>{endpoint}</code></p><p>The <code>report_knowledge_gap</code> tool is available to tokens with <code>feedback:gap</code>. The <code>submit_knowledge_correction</code> tool requires <code>feedback:correction</code> and the project corrections policy to be enabled (currently {correctionsEnabled ? <strong>enabled</strong> : <strong>disabled</strong>}).</p><h4 id="mcp-config-heading">Header configuration</h4><pre aria-labelledby="mcp-config-heading" className="safe-pre">{config}</pre><button type="button" className="quiet-button" onClick={() => void copy(config, 'Configuration')}>Copy configuration</button><h4 id="mcp-claude-heading">Claude Code</h4><pre aria-labelledby="mcp-claude-heading" className="safe-pre">{claude}</pre><button type="button" className="quiet-button" onClick={() => void copy(claude, 'Claude Code command')}>Copy Claude Code command</button>{notice && <p role="status" className="subtle">{notice}</p>}<p className="subtle">Clients see policy or scope changes on their next tool refresh.</p></section>;
}
function ArchivedProjects({ projects, onRestore }: { projects: Project[]; onRestore: (project: Project) => void }) {
  const archived = projects.filter((p) => p.archived_at);
  return <div className="archived-list"><h3>Archived</h3>{archived.length === 0 ? <p className="subtle">No archived projects.</p> : archived.map((project) => <div className="archived-item" key={project.id}><span>{project.name}</span><button className="text-button" onClick={() => onRestore(project)}>Restore</button></div>)}</div>;
}
function ProjectForm({ project, busy, onCancel, onSubmit }: { project: Project | null; busy: boolean; onCancel: () => void; onSubmit: (event: React.FormEvent<HTMLFormElement>) => void }) {
  return <section className="panel" aria-labelledby="project-form-title"><div className="panel-heading"><div><div className="eyebrow">{project ? 'EDIT PROJECT' : 'NEW PROJECT'}</div><h2 id="project-form-title">{project ? 'Project settings' : 'Create a project'}</h2></div></div>
    <form className="project-form" onSubmit={onSubmit}><label htmlFor="project-name">Name</label><input id="project-name" name="name" defaultValue={project?.name ?? ''} maxLength={200} required />
      <label htmlFor="project-slug">Slug</label><input id="project-slug" name="slug" defaultValue={project?.slug ?? ''} pattern="[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,98}[a-zA-Z0-9])?" aria-describedby="slug-help" required /><small id="slug-help" className="subtle">Lowercase letters, numbers, and internal hyphens.</small>
      <label htmlFor="project-description">Description <span className="subtle">(optional)</span></label><textarea id="project-description" name="description" defaultValue={project?.description ?? ''} maxLength={5000} rows={3} />
      <fieldset><legend>Project policies</legend><label className="checkbox-row"><input type="checkbox" name="corrections" defaultChecked={project?.settings.corrections?.enabled ?? false} /> Enable correction feedback</label><p className="help-text">Tokens also need the correction scope before an integration can submit corrections.</p><label htmlFor="deduplication-mode">Deduplication mode</label><select id="deduplication-mode" name="deduplication_mode" defaultValue={project?.settings.deduplication?.mode ?? (project?.settings.deduplication?.enabled === false ? 'disabled' : 'exact')}><option value="disabled">Disabled</option><option value="exact">Exact</option><option value="semantic" disabled>Semantic (Configure an LLM provider first)</option></select><p className="help-text">Exact groups identical questions in the same project and event type. Disabled creates a new group for every submission.</p><p className="help-text">Semantic matching is unavailable until an LLM provider is configured.</p></fieldset>
      {project && <><label className="checkbox-row"><input type="checkbox" name="is_active" defaultChecked={project.is_active} /> Project is active</label></>}
      <div className="button-row"><button disabled={busy}>{busy ? 'Saving…' : project ? 'Save changes' : 'Create project'}</button><button type="button" className="quiet-button" onClick={onCancel}>Cancel</button></div>
    </form></section>;
}

const rootElement = document.getElementById('root');
if (rootElement) createRoot(rootElement).render(<React.StrictMode><App /></React.StrictMode>);
