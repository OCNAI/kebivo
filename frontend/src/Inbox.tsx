import React from 'react';
import { apiRequest } from './api';

type Group = { id: string; project: { id: string; name: string; slug: string }; event_type: string; representative: { event_id: string; question_excerpt: string }; tags: { slug: string; name: string }[]; status: string; occurrence_count: number; first_seen_at: string; last_seen_at: string; delivery_summary: { delivered: number; in_progress: number; failed: number } };
type Page = { items: Group[]; next_cursor: string | null };
type GroupSummary = { total_groups: number; groups_by_status: Record<string, number> };
const statuses = ['open', 'in_progress', 'resolved', 'dismissed'];
const sorts = [['last_activity_desc', 'Last seen (newest)'], ['last_activity_asc', 'Last seen (oldest)'], ['occurrences_desc', 'Most occurrences'], ['occurrences_asc', 'Fewest occurrences'], ['status', 'Status'], ['first_reported_desc', 'First seen (newest)']] as const;
const statusLabels: Record<string, string> = { open: 'Open', in_progress: 'In progress', resolved: 'Resolved', dismissed: 'Dismissed' };
const exactDate = (value: string) => new Date(value).toLocaleString();
function relativeDate(value: string) {
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const units: [number, Intl.RelativeTimeFormatUnit][] = [[31536000, 'year'], [2592000, 'month'], [86400, 'day'], [3600, 'hour'], [60, 'minute']];
  const unit = units.find(([size]) => Math.abs(seconds) >= size);
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(unit ? Math.round(seconds / unit[0]) : seconds, unit?.[1] ?? 'second');
}

export function Inbox({ navigate }: { path?: string; navigate: (url: string) => void }) {
  const params = new URLSearchParams(window.location.search);
  const projectId = params.get('project_id') ?? '';
  const eventType = params.get('event_type') ?? '';
  const status = params.getAll('status');
  const tags = params.getAll('tag');
  const q = params.get('q') ?? '';
  const firstFrom = (params.get('first_seen_from') ?? '').slice(0, 10);
  const firstTo = (params.get('first_seen_to') ?? '').slice(0, 10);
  const lastFrom = (params.get('last_seen_from') ?? '').slice(0, 10);
  const lastTo = (params.get('last_seen_to') ?? '').slice(0, 10);
  const minOccurrences = params.get('min_occurrences') ?? '';
  const sort = params.get('sort') ?? 'last_activity_desc';
  const [projects, setProjects] = React.useState<{ id: string; name: string }[]>([]);
  const [items, setItems] = React.useState<Group[]>([]);
  const [summary, setSummary] = React.useState<GroupSummary | null>(null);
  const [cursor, setCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [moreLoading, setMoreLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const [retry, setRetry] = React.useState(0);
  const [moreOpen, setMoreOpen] = React.useState(false);
  const [mobileFiltersOpen, setMobileFiltersOpen] = React.useState(false);
  const [searchState, setSearchState] = React.useState({ source: q, value: q });
  const searchDraft = searchState.source === q ? searchState.value : q;
  React.useEffect(() => {
    if (searchDraft === q) return;
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams(window.location.search);
      if (searchDraft) next.set('q', searchDraft); else next.delete('q');
      history.replaceState({}, '', `/inbox${next.size ? `?${next}` : ''}`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    }, 300);
    return () => window.clearTimeout(timer);
  }, [searchDraft, q]);

  React.useEffect(() => { let active = true; apiRequest<{ items: { id: string; name: string }[] }>('projects?limit=100').then(r => { if (active) setProjects(r.items); }).catch(() => {}); return () => { active = false; }; }, []);
  React.useEffect(() => { let active = true; const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''; apiRequest<GroupSummary>(`admin/groups/summary${query}`).then(result => { if (active) setSummary(result); }).catch(() => {}); return () => { active = false; }; }, [projectId]);
  const filterKey = [projectId, eventType, status.join(','), tags.join(','), q, firstFrom, firstTo, lastFrom, lastTo, minOccurrences, sort].join('|');
  const filterKeyRef = React.useRef(filterKey);
  React.useLayoutEffect(() => { filterKeyRef.current = filterKey; }, [filterKey]);
  React.useEffect(() => {
    let active = true;
    const query = new URLSearchParams(window.location.search);
    query.set('limit', '50'); query.delete('cursor');
    apiRequest<Page>(`admin/groups?${query}`).then(result => { if (active) { setItems(result.items ?? []); setCursor(result.next_cursor ?? null); setError(''); } }).catch(e => { if (active) setError(e instanceof Error ? e.message : 'Could not load groups.'); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [filterKey, retry]);

  function update(key: string, value: string, replace = false) {
    setError(''); setLoading(true);
    const next = new URLSearchParams(window.location.search);
    if (replace) next.delete(key);
    if (value) { if (key === 'tag') next.append(key, value); else next.set(key, value); } else if (!replace) next.delete(key);
    navigate(`/inbox${next.size ? `?${next}` : ''}`);
  }
  function setStatuses(next: string[]) {
    setError(''); setLoading(true);
    const params = new URLSearchParams(window.location.search); params.delete('status'); next.forEach(value => params.append('status', value));
    navigate(`/inbox${params.size ? `?${params}` : ''}`);
  }
  const availableTags = Array.from(new Map(items.flatMap(item => item.tags).map(tag => [tag.slug, tag])).values());
  function clearFilters() { navigate('/inbox'); setLoading(true); setError(''); }
  function removeTag(tag: string) { setLoading(true); setError(''); const params = new URLSearchParams(window.location.search); params.delete('tag'); tags.filter(item => item !== tag).forEach(item => params.append('tag', item)); navigate(`/inbox${params.size ? `?${params}` : ''}`); }
  async function loadMore() {
    if (!cursor) return; const requestedFilterKey = filterKey; setMoreLoading(true); setError('');
    const query = new URLSearchParams(window.location.search); query.set('limit', '50'); query.set('cursor', cursor);
    try { const result = await apiRequest<Page>(`admin/groups?${query}`); if (filterKeyRef.current !== requestedFilterKey) return; setItems(current => { const ids = new Set(current.map(item => item.id)); const appended = result.items.filter(item => { if (ids.has(item.id)) return false; ids.add(item.id); return true; }); return [...current, ...appended]; }); setCursor(result.next_cursor); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not load more groups.'); } finally { setMoreLoading(false); }
  }
  const filtered = Boolean(projectId || eventType || status.length || tags.length || q || firstFrom || firstTo || lastFrom || lastTo || minOccurrences);
  const moreFilterCount = Number(Boolean(firstFrom || firstTo)) + Number(Boolean(lastFrom || lastTo)) + Number(Boolean(minOccurrences)) + tags.length;
  const statusCounts = summary?.groups_by_status ?? {};
  const statusCountLabel = statuses.map(value => `${statusLabels[value]} ${statusCounts[value] ?? 0}`).join(', ');
  return <main className="dashboard-main"><section className="dashboard" aria-labelledby="inbox-title">
    <div className="dashboard-heading"><div><div className="eyebrow">FEEDBACK</div><h1 id="inbox-title" tabIndex={-1}>Group inbox</h1><p className="subtle">Recurring questions, grouped by project and type.</p></div></div>
    {error && <div role="alert" className="error-message"><span>{error}</span> <button className="quiet-button" onClick={() => { setLoading(true); setRetry(v => v + 1); }}>Retry</button></div>}
    <section className="panel inbox-filter-panel" aria-label="Group inbox filters">
      <button type="button" className="quiet-button mobile-filter-toggle" aria-expanded={mobileFiltersOpen} onClick={() => setMobileFiltersOpen(value => !value)}>Filters{filtered ? ` (${Number(Boolean(projectId))+Number(Boolean(eventType))+status.length+tags.length+Number(Boolean(q))+Number(Boolean(firstFrom||firstTo))+Number(Boolean(lastFrom||lastTo))+Number(Boolean(minOccurrences))})` : ''}</button>
      <div className={`inbox-filter-content${mobileFiltersOpen ? ' is-open' : ''}`}>
      <div className="group-filters">
        <label className="wide-filter">Search<input aria-label="Search groups" type="search" value={searchDraft} onChange={e => setSearchState({ source: q, value: e.target.value })} placeholder="Search questions" /></label>
        <label>Project<select aria-label="Filter by project" value={projectId} onChange={e => update('project_id', e.target.value)}><option value="">All projects</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Type<select aria-label="Filter by type" value={eventType} onChange={e => update('event_type', e.target.value)}><option value="">All types</option><option value="knowledge_gap">Knowledge gap</option><option value="knowledge_correction">Knowledge correction</option></select></label>
        <fieldset className="status-filter"><legend>Status</legend><div role="group" aria-label={`Filter by status. Total groups by status: ${statusCountLabel}`} className="status-toggles">{statuses.map(value => <button type="button" key={value} aria-pressed={status.includes(value)} className={`status-toggle${status.includes(value) ? ' is-selected' : ''}`} onClick={() => setStatuses(status.includes(value) ? status.filter(item => item !== value) : [...status, value])}>{statusLabels[value]} <span>{statusCounts[value] ?? 0}</span></button>)}</div></fieldset>
        <label>Sort<select aria-label="Sort groups" value={sort} onChange={e => update('sort', e.target.value)}>{sorts.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <details className="more-filters" open={moreOpen} onToggle={e => setMoreOpen(e.currentTarget.open)}><summary>More filters{moreFilterCount ? <span className="filter-count">{moreFilterCount}</span> : null}</summary>
        <div className="more-filter-grid"><label>Minimum occurrences<input aria-label="Minimum occurrences" type="number" min="1" value={minOccurrences} onChange={e => update('min_occurrences', e.target.value)} /></label>
        {([['first_seen_from','First seen from (UTC day)',firstFrom],['first_seen_to','First seen to (UTC day)',firstTo],['last_seen_from','Last seen from (UTC day)',lastFrom],['last_seen_to','Last seen to (UTC day)',lastTo]] as const).map(([key,label,value]) => <label key={key}>{label}<input aria-label={label} type="date" value={value} onChange={e => update(key, e.target.value ? `${e.target.value}T${key.endsWith('_to') ? '23:59:59' : '00:00:00'}Z` : '')} /></label>)}
        <label>Tag<select aria-label="Filter by existing tag" value="" onChange={e => e.target.value && update('tag', e.target.value)}><option value="">Choose a tag…</option>{availableTags.filter(tag => !tags.includes(tag.slug)).map(tag => <option key={tag.slug} value={tag.slug}>{tag.name}</option>)}</select></label></div>
      </details>
      <div className="filter-chips" aria-label="Active filters">{status.map(s => <button key={s} type="button" className="scope-chip" onClick={() => setStatuses(status.filter(value => value !== s))} aria-label={`Remove status ${statusLabels[s]}`}>Status: {statusLabels[s]} ×</button>)}{tags.map(tag => <button key={tag} type="button" className="scope-chip" onClick={() => removeTag(tag)} aria-label={`Remove tag ${tag}`}>Tag: {availableTags.find(item => item.slug === tag)?.name ?? tag} ×</button>)}</div>
      </div>
      <div className="filter-summary"><p aria-live="polite" aria-atomic="true">{loading ? 'Loading groups…' : filtered ? `Showing ${items.length} groups` : `Showing ${items.length} of ${summary?.total_groups ?? items.length} groups`}</p>{filtered && <button type="button" className="text-button" onClick={clearFilters}>Clear filters</button>}</div>
      {loading ? <p role="status">Loading groups…</p> : !error && items.length === 0 ? <div className="empty-state"><h2>{filtered ? 'No matching groups' : 'No feedback groups yet'}</h2><p>{filtered ? 'Your filters hide all results. Try changing or clearing them.' : projects[0] ? 'Send a test event from a project Connect panel to see the full path.' : 'Create a project, then send a test event from its Connect panel to see the full path.'}</p>{!filtered&&projects[0]&&<button className="quiet-button" onClick={()=>navigate(`/?project=${encodeURIComponent(projects[0].id)}`)}>Open Connect panel</button>}</div> : items.length > 0 && <><GroupTable items={items} />{cursor && <button className="quiet-button" onClick={() => void loadMore()} disabled={moreLoading}>{moreLoading ? 'Loading…' : 'Load more'}</button>}</>}
    </section>
  </section></main>;
}

export function GroupTable({ items }: { items: Group[] }) {
  const normalizeQuestion = (question: string) => question.normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  const counts = new Map<string, number>();
  items.forEach(item => {
    const key = normalizeQuestion(item.representative.question_excerpt);
    if (key) counts.set(key, (counts.get(key) ?? 0) + 1);
  });
  const duplicateCount = (item: Group) => counts.get(normalizeQuestion(item.representative.question_excerpt)) ?? 0;
  return <><div className="table-wrap"><table className="inbox-table group-table"><caption className="sr-only">Feedback groups</caption><thead><tr><th>Question</th><th>Project</th><th>Type</th><th>Tags</th><th>Status</th><th>Occurrences</th><th>First seen</th><th>Last seen</th><th>Delivery</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td className="question-cell safe-text" dir="auto"><a title={item.representative.question_excerpt} href={`/inbox/groups/${item.id}`}>{item.representative.question_excerpt}</a>{duplicateCount(item) > 1 && <span className="possible-duplicates">Possible duplicates ({duplicateCount(item)})</span>}</td><td className="safe-text" dir="auto">{item.project.name}</td><td>{item.event_type === 'knowledge_gap' ? 'Knowledge gap' : 'Correction'}</td><td>{item.tags.map(tag => <span key={tag.slug} dir="auto" className="scope-chip safe-text">{tag.name}</span>)}</td><td><span className={`status status-${item.status}`}>{statusLabels[item.status] ?? item.status}</span></td><td>{item.occurrence_count}</td><td><time dateTime={item.first_seen_at} title={exactDate(item.first_seen_at)}>{relativeDate(item.first_seen_at)}</time></td><td><time dateTime={item.last_seen_at} title={exactDate(item.last_seen_at)}>{relativeDate(item.last_seen_at)}</time></td><td>{item.delivery_summary.failed > 0 ? <span className="delivery-badge delivery-failed">{item.delivery_summary.failed} failed</span> : item.delivery_summary.in_progress > 0 ? <span className="delivery-badge delivery-active">{item.delivery_summary.in_progress} active</span> : <span className="sr-only">No active or failed deliveries</span>}</td></tr>)}</tbody></table></div><div className="group-cards">{items.map(item => <article className="group-card" key={item.id}><a className="group-card-question" href={`/inbox/groups/${item.id}`} title={item.representative.question_excerpt}>{item.representative.question_excerpt}</a>{duplicateCount(item) > 1 && <span className="possible-duplicates">Possible duplicates ({duplicateCount(item)})</span>}<div className="group-card-tags">{item.tags.map(tag => <span key={tag.slug} className="scope-chip">{tag.name}</span>)}</div><p>{item.project.name} · {item.event_type === 'knowledge_gap' ? 'Knowledge gap' : 'Correction'} · <span className={`status status-${item.status}`}>{statusLabels[item.status] ?? item.status}</span> · {item.occurrence_count} {item.occurrence_count === 1 ? 'occurrence' : 'occurrences'}</p><p><time dateTime={item.last_seen_at} title={exactDate(item.last_seen_at)}>{relativeDate(item.last_seen_at)}</time>{item.delivery_summary.failed > 0 ? ` · ${item.delivery_summary.failed} failed` : item.delivery_summary.in_progress > 0 ? ` · ${item.delivery_summary.in_progress} active` : ''}</p></article>)}</div></>;
}
