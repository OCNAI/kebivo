import React from 'react';
import { apiRequest } from './api';
import { exactDate, GroupTable, relativeDate } from './Inbox';
import { SampleBadge } from './FirstRun';

type Summary = { total_groups: number; total_events: number; groups_by_status: Record<string, number>; failed_deliveries_last_24h: number; last_7d_from?: string; resolved_last_7d?: number; reopened_last_7d?: number; top_open_gaps: React.ComponentProps<typeof GroupTable>['items'] };
export function FirstRunChecklist({ projects, navigate, onShownChange }: { projects: { id: string; name: string }[]; navigate: (url: string) => void; onShownChange?: (shown: boolean) => void }) {
  const [hasEvents, setHasEvents] = React.useState<boolean | null>(null);
  const [hasToken, setHasToken] = React.useState(false);

  React.useEffect(() => {
    let active = true;
    apiRequest<Summary>('admin/groups/summary')
      .then(summary => { if (active) setHasEvents(summary.total_events > 0); })
      .catch(() => { if (active) setHasEvents(false); });
    if (projects[0]) {
      apiRequest<{ items: unknown[] }>(`projects/${projects[0].id}/tokens`)
        .then(result => { if (active) setHasToken(result.items.length > 0); })
        .catch(() => {});
    }
    return () => { active = false; };
  }, [projects]);
  React.useEffect(() => { if (hasEvents !== null) onShownChange?.(!hasEvents); }, [hasEvents, onShownChange]);

  if (hasEvents !== false) return null;
  return <section className="panel first-run" aria-labelledby="first-run-title">
    <div className="eyebrow">FIRST RUN</div>
    <h2 id="first-run-title">Get your first event in</h2>
    <p>Four steps, about two minutes.</p>
    <ol>
      <li><span className="sr-only">{projects.length ? 'Done: ' : 'To do: '}</span>Create a project{!projects.length && <button className="text-button" onClick={() => document.querySelector<HTMLButtonElement>('.project-panel .panel-heading button')?.click()}>Create project</button>}</li>
      <li><span className="sr-only">{hasToken ? 'Done: ' : 'To do: '}</span>Create a token{!hasToken && projects[0] && <button className="text-button" onClick={() => navigate(`/?project=${encodeURIComponent(projects[0].id)}`)}>Open project settings</button>}</li>
      <li><span className="sr-only">To do: </span>Send a test event — <button className="text-button" onClick={() => navigate(`/?project=${encodeURIComponent(projects[0]?.id ?? '')}`)}>Copy the REST example from the project page</button></li>
      <li><span className="sr-only">To do: </span>See it in the inbox — <button className="text-button" onClick={() => navigate('/inbox')}>Open group inbox</button></li>
    </ol>
  </section>;
}
type Tile = { label: string; hint?: string; count?: number; url?: string; danger?: boolean };
function TileGroup({ id, title, tiles, navigate }: { id: string; title: string; tiles: Tile[]; navigate: (url: string) => void }) {
  return <div className="summary-group" role="group" aria-labelledby={id}><h3 id={id}>{title}</h3><div className="summary-counts">{tiles.map(tile => {
    const body = <><strong>{tile.danger && <i className="summary-alert" aria-hidden="true">⚠&#xFE0E;</i>}{tile.count ?? <span className="summary-skeleton" />}</strong><span>{tile.label}{tile.hint && <span className="sr-only"> {tile.hint}</span>}</span></>;
    const className = tile.danger ? 'summary-tile danger' : 'summary-tile';
    return tile.url ? <a key={tile.label} className={className} href={tile.url} onClick={e => { e.preventDefault(); navigate(tile.url!); }}>{body}</a> : <div key={tile.label} className={className}>{body}</div>;
  })}</div></div>;
}
export function DashboardSummary({ navigate }: { navigate: (url: string) => void }) {
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [error, setError] = React.useState('');
  const [retry, setRetry] = React.useState(0);
  React.useEffect(() => { const refresh = () => setRetry(value => value + 1); addEventListener('sample-data-refresh', refresh); return () => removeEventListener('sample-data-refresh', refresh); }, []);
  React.useEffect(() => { let active = true; apiRequest<Summary>('admin/groups/summary').then(value => { if (active) { setSummary({ total_groups: value.total_groups ?? 0, total_events: value.total_events ?? 0, groups_by_status: value.groups_by_status ?? {}, failed_deliveries_last_24h: value.failed_deliveries_last_24h ?? 0, last_7d_from: value.last_7d_from, resolved_last_7d: value.resolved_last_7d ?? 0, reopened_last_7d: value.reopened_last_7d ?? 0, top_open_gaps: value.top_open_gaps ?? [] }); setError(''); } }).catch(e => { if (active) setError(e instanceof Error ? e.message : 'Could not load dashboard summary.'); }); return () => { active = false; }; }, [retry]);
  const loaded = Boolean(summary);
  const since = summary?.last_7d_from ? `&status_changed_from=${encodeURIComponent(summary.last_7d_from)}` : '';
  const status = (key: string, label: string): Tile => ({ label, count: summary ? summary.groups_by_status[key] ?? 0 : undefined, url: loaded ? `/inbox?status=${key}` : undefined });
  const failures = summary?.failed_deliveries_last_24h;
  const now: Tile[] = [status('open', 'Open'), status('in_progress', 'In progress'), { label: 'Delivery failures (24h)', count: failures, danger: Boolean(failures), url: failures ? '/deliveries?status=failed' : undefined }];
  const week: Tile[] = [{ label: 'Resolved', hint: 'in the last 7 days', count: summary?.resolved_last_7d, url: loaded ? `/inbox?status=resolved${since}` : undefined }, { label: 'Reopened', hint: 'in the last 7 days', count: summary?.reopened_last_7d, url: loaded ? `/inbox?reopened=true${since}` : undefined }];
  const total: Tile[] = [{ label: 'Groups', count: summary?.total_groups, url: loaded ? '/inbox' : undefined }, { label: 'Events', count: summary?.total_events, url: loaded ? '/inbox' : undefined }, status('resolved', 'Resolved'), status('dismissed', 'Dismissed')];
  const recurring = summary?.top_open_gaps.filter(group => group.occurrence_count > 1) ?? [];
  return <section className="panel group-summary" aria-labelledby="feedback-summary-title"><div className="panel-heading"><div><div className="eyebrow">FEEDBACK</div><h2 id="feedback-summary-title">Feedback summary</h2><p className="subtle">Across all projects.</p></div><a className="app-link" href="/inbox" onClick={e => { e.preventDefault(); navigate('/inbox'); }}>Open group inbox</a></div>
    {error && <div role="alert" className="error-message"><span>{error}</span> <button className="quiet-button" onClick={() => setRetry(v => v + 1)}>Retry</button></div>}
    <p role="status" className="sr-only">{!summary && !error ? 'Loading feedback summary…' : ''}</p>
    {(summary || !error) && <><div className="summary-groups" aria-hidden={summary ? undefined : true}><TileGroup id="summary-now" title="Now" tiles={now} navigate={navigate} /><TileGroup id="summary-week" title="Last 7 days" tiles={week} navigate={navigate} /><TileGroup id="summary-total" title="All time" tiles={total} navigate={navigate} /></div>
      <div className="panel-heading recurring-heading"><div><h3>Top recurring open gaps</h3><p className="subtle">Most frequently reported unresolved questions.</p></div><a className="app-link" href="/inbox?status=open&amp;event_type=knowledge_gap" onClick={e => { e.preventDefault(); navigate('/inbox?status=open&event_type=knowledge_gap'); }}>View all open gaps</a></div>
      {!summary ? <div className="recurring-skeleton" aria-hidden="true" /> : summary.total_events===0 ? null : recurring.length ? <div className="table-wrap"><table className="recurring-table"><caption className="sr-only">Top recurring open questions</caption><thead><tr><th scope="col">Question</th><th scope="col">Project</th><th scope="col">Occurrences</th><th scope="col">Last seen</th></tr></thead><tbody>{recurring.map(group => <tr key={group.id}><th scope="row"><a href={`/inbox/groups/${group.id}`} onClick={e => { e.preventDefault(); navigate(`/inbox/groups/${group.id}`); }}>{group.representative.question_excerpt}</a></th><td>{group.project.name} {group.project.is_sample && <SampleBadge />}</td><td>{group.occurrence_count}</td><td><time dateTime={group.last_seen_at} title={exactDate(group.last_seen_at)}>{relativeDate(group.last_seen_at)}</time></td></tr>)}</tbody></table></div> : <p className="subtle">No question has been reported more than once yet.</p>}</>}
  </section>;
}
