import React from 'react';
import { ApiError, apiRequest, setCsrfToken } from './api';

export type SampleStatus = { present: boolean; has_real_events: boolean; has_real_projects: boolean };
type FirstRunContextValue = { sample: SampleStatus | null; refreshSample: () => Promise<SampleStatus | null>; sampleError: boolean; announce: (message: string) => void };
const FirstRunContext = React.createContext<FirstRunContextValue | null>(null);
export function useFirstRun() { return React.useContext(FirstRunContext); }

export function FirstRunProvider({ children, navigate }: { children: React.ReactNode; navigate: (url: string) => void }) {
  const [sample, setSample] = React.useState<SampleStatus | null>(null);
  const [sampleError, setSampleError] = React.useState(false);
  const [notice, setNotice] = React.useState('');
  const refreshSample = React.useCallback(async () => {
    try { const value = await apiRequest<SampleStatus>('admin/sample-data'); setSample(value); setSampleError(false); return value; }
    catch { setSample(null); setSampleError(true); return null; }
  }, []);
  React.useEffect(() => { const refresh = () => void refreshSample(); void Promise.resolve().then(refreshSample); addEventListener('sample-data-refresh', refresh); return () => removeEventListener('sample-data-refresh', refresh); }, [refreshSample]);
  React.useEffect(() => { const listener = (event: Event) => setNotice((event as CustomEvent<string>).detail); addEventListener('first-run-notice', listener); return () => removeEventListener('first-run-notice', listener); }, []);
  const value = React.useMemo(() => ({ sample, refreshSample, sampleError, announce: setNotice }), [sample, refreshSample, sampleError]);
  return <FirstRunContext.Provider value={value}>{sample?.present && <SampleBanner sample={sample} refresh={refreshSample} navigate={navigate} announce={setNotice} />}{notice && <p role="status" className="sample-announcement" aria-live="polite">{notice}</p>}{children}</FirstRunContext.Provider>;
}

function SampleBanner({ sample, refresh, navigate, announce }: { sample: SampleStatus; refresh: () => Promise<SampleStatus | null>; navigate: (url: string) => void; announce: (message: string) => void }) {
  const dialog = React.useRef<HTMLDialogElement>(null), keep = React.useRef<HTMLButtonElement>(null), opener = React.useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = React.useState(false), [error, setError] = React.useState('');
  function open(event: React.MouseEvent<HTMLButtonElement>) { opener.current = event.currentTarget; setError(''); dialog.current?.showModal(); requestAnimationFrame(() => keep.current?.focus()); }
  async function remove() {
    setBusy(true); setError('');
    try { await apiRequest('admin/sample-data', { method: 'DELETE' }); window.dispatchEvent(new Event('sample-data-refresh')); dialog.current?.close(); await refresh(); announce('Sample data removed.'); navigate('/inbox'); requestAnimationFrame(() => document.querySelector<HTMLElement>('main h1')?.focus()); }
    catch { setError("Couldn't remove sample data. Nothing was changed. Try again."); }
    finally { setBusy(false); }
  }
  return <>
    <section className="sample-banner" aria-label="Sample data">
      <p>{sample.has_real_projects ? <><strong>Sample data is still here.</strong> Remove it when you've seen enough. Your own projects and feedback stay.</> : <><strong>You're looking at sample data.</strong> The projects, feedback and destinations marked Sample are made up. Sample destinations never send anything.</>}</p>
      <div className="sample-actions">{!sample.has_real_projects && <button type="button" onClick={() => navigate('/?create=1')}>Connect your own app</button>}<button ref={opener} className="quiet-button" type="button" onClick={open}>Remove sample data</button></div>
    </section>
    <dialog className="sample-dialog" ref={dialog} aria-labelledby="sample-dialog-title" aria-describedby="sample-dialog-description" onCancel={event => { if (busy) event.preventDefault(); }} onClick={event => { if (event.target === dialog.current && !busy) dialog.current.close(); }} onClose={() => { setError(''); requestAnimationFrame(() => opener.current?.focus()); }}>
      <h2 id="sample-dialog-title">Remove sample data?</h2><p id="sample-dialog-description">This deletes the sample projects, feedback groups, notes and destinations. Anything you added yourself stays. Sample data can be loaded again only while your inbox has no real feedback.</p>
      {error && <p className="error-message" role="alert">{error}</p>}
      <div className="sample-dialog-actions"><button ref={keep} className="quiet-button" type="button" disabled={busy} onClick={() => dialog.current?.close()}>Keep it</button><button className="danger-button" type="button" disabled={busy} onClick={() => void remove()}>{busy ? 'Removing…' : 'Remove sample data'}</button></div>
    </dialog>
  </>;
}

export function FirstRunEmpty({ navigate, dashboard = false, onLoaded, projectId }: { navigate: (url: string) => void; dashboard?: boolean; onLoaded?: () => void; projectId?: string }) {
  const context = useFirstRun();
  const [totalEvents, setTotalEvents] = React.useState<number | null>(dashboard ? null : 0);
  const [busy, setBusy] = React.useState(false), [error, setError] = React.useState(''), [notice, setNotice] = React.useState('');
  React.useEffect(() => { if (!dashboard) return; let live = true; apiRequest<{ total_events: number }>('admin/groups/summary').then(value => { if (live) setTotalEvents(value.total_events); }).catch(() => { if (live) setTotalEvents(-1); }); return () => { live = false; }; }, [dashboard]);
  const filtered = dashboard ? false : Boolean(new URLSearchParams(location.search).size);
  if (filtered || context?.sample?.present || (dashboard && totalEvents !== 0 && totalEvents !== null)) return null;
  const loading = (dashboard && totalEvents === null) || (context?.sample === null && !context?.sampleError);
  const variantB = context?.sample?.has_real_events;
  async function load() {
    setBusy(true); setError(''); setNotice('');
    try { await apiRequest('admin/sample-data', { method: 'POST', body: '{}' }); await context?.refreshSample(); window.dispatchEvent(new Event('sample-data-refresh')); setNotice('Sample data loaded.'); onLoaded?.(); requestAnimationFrame(() => document.querySelector<HTMLElement>('main h1')?.focus()); }
    catch (e) {
      if (e instanceof ApiError && e.status === 409) { await context?.refreshSample(); window.dispatchEvent(new Event('sample-data-refresh')); return; }
      setError(e instanceof ApiError && e.status === 401 ? 'Your session has expired. Sign in again to continue.' : "Couldn't load sample data. Nothing was added. Try again.");
    } finally { setBusy(false); }
  }
  return <section className="first-run-empty" aria-busy={busy || undefined}>
    {notice && <p role="status">{notice}</p>}
    {loading ? <p role="status">Loading sample data options…</p> : <>
      <h2>No feedback yet</h2>
      <p>{variantB ? 'Send a test event from a project Connect panel to see the full path.' : "Kebivo groups the questions your app or agent couldn't answer, so you can see what is missing. Look around with sample data first, or connect your own app."}</p>
      <div className="button-row">{!variantB && <button type="button" disabled={busy} onClick={() => void load()}>{busy ? 'Loading sample data…' : 'Load sample data'}</button>}<button type="button" className="quiet-button" onClick={() => navigate(projectId ? `/?project=${encodeURIComponent(projectId)}` : '/?create=1')}>Connect your own app</button></div>
      {!variantB && <p className="subtle">Sample data is made up and marked as sample. Sample destinations never send anything. Remove it whenever you like.</p>}
    </>}
    {busy && <p role="status">Loading sample data…</p>}{error && <p role="alert" className="error-message">{error}</p>}
  </section>;
}

export function SampleBadge() { return <span className="badge sample">Sample<span className="sr-only"> data</span></span>; }

export function CommandBlock({ command }: { command: string }) {
  const [status, setStatus] = React.useState('');
  async function copy() { try { await navigator.clipboard.writeText(command); setStatus('Command copied.'); } catch { setStatus('Could not copy. Select the text and copy it manually.'); } }
  return <><pre className="setup-command" tabIndex={0}><code>{command}</code></pre><button type="button" className="quiet-button command-copy" onClick={() => void copy()}>Copy command</button>{status && <p role="status">{status}</p>}</>;
}

export function SetupPage({ token, onSuccess }: { token: string | null; onSuccess: (admin: { id: string; email: string }, csrf: string, sampleLoaded: boolean, sampleAttempted: boolean) => void | Promise<void> }) {
  const [required, setRequired] = React.useState<boolean | null>(null), [networkError, setNetworkError] = React.useState(false);
  const [busy, setBusy] = React.useState(false), [error, setError] = React.useState(''), [fieldError, setFieldError] = React.useState(''), [emailError, setEmailError] = React.useState(''), [clientPasswordError, setClientPasswordError] = React.useState(''), [showPassword, setShowPassword] = React.useState(false);
  const [email, setEmail] = React.useState(''), [password, setPassword] = React.useState(''), [withSample, setWithSample] = React.useState(true), [invalid, setInvalid] = React.useState(false), [created, setCreated] = React.useState(false);
  const title = React.useRef<HTMLHeadingElement>(null), emailInput = React.useRef<HTMLInputElement>(null), passwordInput = React.useRef<HTMLInputElement>(null);
  const loadSetupStatus = React.useCallback(() => apiRequest<{ required: boolean }>('auth/setup').then(value => setRequired(value.required)).catch(() => setNetworkError(true)), []);
  const check = () => { setNetworkError(false); void loadSetupStatus(); };
  React.useEffect(() => { void loadSetupStatus(); }, [loadSetupStatus]);
  React.useEffect(() => { title.current?.focus(); }, [required, invalid]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(''); setFieldError('');
    const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    const invalidPassword = password.length < 12;
    setEmailError(validEmail ? '' : 'Enter a valid email address.');
    setClientPasswordError(invalidPassword ? 'Use at least 12 characters. A few random words work well.' : '');
    if (!validEmail || invalidPassword) { const count = Number(!validEmail) + Number(invalidPassword); setError(`Fix ${count} ${count === 1 ? 'problem' : 'problems'} to continue.`); if (!validEmail) emailInput.current?.focus(); else passwordInput.current?.focus(); return; }
    setBusy(true);
    try {
      const session = await apiRequest<{ admin: { id: string; email: string }; csrf_token: string }>('auth/setup', { method: 'POST', body: JSON.stringify({ token, email, password }) });
      setCsrfToken(session.csrf_token); setCreated(true);
      let sampleLoaded = false;
      if (withSample) { try { await apiRequest('admin/sample-data', { method: 'POST', body: '{}' }); sampleLoaded = true; } catch { /* Setup succeeded; the empty-state action retries sample loading. */ } }
      await onSuccess(session.admin, session.csrf_token, sampleLoaded, withSample);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403 && e.message === 'Invalid or expired setup link') { setInvalid(true); return; }
      if (e instanceof ApiError && e.status === 409) { setRequired(false); return; }
      if (e instanceof ApiError && e.status === 403) { setError("Kebivo doesn't accept setup from this address. Open the setup link using the same address and port you use to reach Kebivo. If that isn't localhost, set ALLOWED_ORIGINS to it and restart the container."); return; }
      if (e instanceof ApiError && e.status === 422) { const emailIssue = e.fields.find(x => x.field.includes('email')); const passwordIssue = e.fields.find(x => x.field.includes('password')); if (emailIssue) setEmailError(emailIssue.message); setFieldError(passwordIssue?.message ?? (!emailIssue ? e.message : '')); (emailIssue && !passwordIssue ? emailInput : passwordInput).current?.focus(); return; }
      setError("Couldn't create the account. Nothing was saved. Try again.");
    } finally { setBusy(false); }
  }
  if (networkError) return <main className="center"><section className="welcome-card"><p role="alert">Couldn't reach Kebivo. Check that the container is running, then Retry.</p><button className="quiet-button" onClick={check}>Retry</button></section></main>;
  if (required === null) return <main className="center"><section className="welcome-card"><p role="status">Checking setup…</p></section></main>;
  if (!required || created) return <main className="center"><section className="welcome-card"><h1 ref={title} tabIndex={-1}>This server is already set up</h1><p>An administrator account exists. Sign in to continue.</p><a className="app-link" href="/">Go to sign in</a></section></main>;
  if (invalid || !token) return <main className="center"><section className="welcome-card"><div className="eyebrow">FIRST-TIME SETUP</div><h1 ref={title} tabIndex={-1}>This setup link doesn't work</h1><p>Setup links expire after 60 minutes and stop working once the server restarts. Restart the container to print a new one, then open it.</p><p>Just reloaded this page? The link from the logs still works if it hasn't expired. Open it again.</p><CommandBlock command={'docker restart kebivo\ndocker logs kebivo'} /><p>If your container has a different name, use that instead. Use the last line that starts with <code>Kebivo setup:</code>. Earlier ones no longer work.</p><a className="app-link" href="/">Already set up? Sign in</a></section></main>;
  return <main className="center"><section className="welcome-card" aria-labelledby="setup-title"><div className="eyebrow">FIRST-TIME SETUP</div><h1 id="setup-title" ref={title} tabIndex={-1}>Create your administrator account</h1><p className="intro">This account manages everything in this Kebivo server. You only do this once.</p>{error && !error.startsWith('Fix ') && <p role="alert" className="error-message">{error}</p>}
    <form onSubmit={submit} aria-busy={busy} noValidate>{error.startsWith('Fix ') && <p role="alert" className="error-message">{error}</p>}<label htmlFor="setup-email">Email address</label><input ref={emailInput} id="setup-email" type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} aria-describedby={emailError ? 'email-error' : undefined} aria-invalid={Boolean(emailError) || undefined}/>{emailError && <small id="email-error" className="field-error">{emailError}</small>}
      <label htmlFor="setup-password">Password</label><input ref={passwordInput} id="setup-password" type={showPassword ? 'text' : 'password'} autoComplete="new-password" required value={password} onChange={e => setPassword(e.target.value)} aria-describedby={`password-hint${clientPasswordError || fieldError ? ' password-error' : ''}`} aria-invalid={Boolean(clientPasswordError || fieldError) || undefined}/><small id="password-hint">At least 12 characters.</small>{(clientPasswordError || fieldError) && <small id="password-error" className="field-error">{clientPasswordError || fieldError}</small>}
      <label className="checkbox-row"><input type="checkbox" checked={showPassword} onChange={e => setShowPassword(e.target.checked)}/>Show password</label>
      <label className="checkbox-row"><input type="checkbox" checked={withSample} aria-describedby="sample-hint" onChange={e => setWithSample(e.target.checked)}/><span><strong>Start with sample data</strong><small id="sample-hint">Adds made-up projects and feedback from the last two weeks so you can look around. It is clearly marked, and you can remove it any time.</small></span></label>
      <button disabled={busy}>{busy ? 'Creating account…' : 'Create account'}</button>
    </form><p className="fine-print">Prefer the command line? See the <a className="app-link" href="https://docs.kebivo.com/">server documentation</a>.</p>
  </section></main>;
}

export function takeSetupToken(path: string, hash: string): string | null {
  if (path !== '/setup' || !hash) return null;
  return new URLSearchParams(hash.slice(1)).get('token');
}

export function takeSetupTokenFromLocation(): string | null {
  const token = takeSetupToken(location.pathname, location.hash);
  if (location.pathname === '/setup' && location.hash) history.replaceState({}, '', location.pathname + location.search);
  return token;
}
