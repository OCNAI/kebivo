import React from "react";
import { apiRequest, ApiError } from "./api";

type Event = {
  id: string;
  event_type: string;
  agent_reported: Record<string, unknown>;
  submitted_correction?: Record<string, unknown>;
  system: Record<string, unknown>;
  tags: {
    slug: string;
    name: string;
    from_default: boolean;
    from_submission: boolean;
    from_admin: boolean;
  }[];
  created_at?: string;
  match?: { method: "initial" | "exact" | "semantic" | "manual_merge" | "manual_split"; triggered_reopen: boolean };
  semantic?: { outcome: string; confidence?: number | null; reason?: string | null; error_category?: string | null } | null;
};
type Group = {
  id: string;
  status: string;
  occurrence_count: number;
  representative: { event_id: string; question: string };
  project: { id: string; name: string };
  event_type: string;
  notes: { id: string; body: string; created_at: string }[];
  tags: { slug: string; name: string }[];
};
type Delivery = {
  id: string;
  event_id: string;
  status: string;
  attempt_count: number;
  destination_id: string;
  last_error_message?: string | null;
};
type DeliveryPage = { items: Delivery[]; next_cursor: string | null };
const statusLabels: Record<string, string> = { open: "Open", in_progress: "In progress", resolved: "Resolved", dismissed: "Dismissed" };
const matchLabels: Record<string, string> = { initial: "Initial", exact: "Exact match", semantic: "Semantic match", manual_merge: "Merged", manual_split: "Split" };
const stringify = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);
const safe = (value: unknown) => (
  <pre className="safe-pre safe-text" dir="auto">
    {stringify(value)}
  </pre>
);

export function GroupDetail({
  groupId,
  navigate,
}: {
  groupId: string;
  navigate: (url: string) => void;
}) {
  const [group, setGroup] = React.useState<Group | null>(null),
    [events, setEvents] = React.useState<Event[]>([]),
    [history, setHistory] = React.useState<
      { id: string; action: string; created_at: string; actor_type?: string; from_status?: string | null; to_status?: string | null; details?: Record<string, unknown> }[]
    >([]),
    [deliveries, setDeliveries] = React.useState<Delivery[]>([]),
    [deliveryCursor, setDeliveryCursor] = React.useState<string | null>(null),
    [deliveryLoading, setDeliveryLoading] = React.useState(false),
    [attempts, setAttempts] = React.useState<Record<string, unknown> | null>(
      null,
    ),
    [error, setError] = React.useState(""),
    [loading, setLoading] = React.useState(true),
    [more, setMore] = React.useState<string | null>(null),
    [note, setNote] = React.useState(""),
    [tag, setTag] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [deleteNoteId, setDeleteNoteId] = React.useState<string | null>(null);
  const [questionExpanded, setQuestionExpanded] = React.useState(false);
  const [dialog, setDialog] = React.useState<"merge" | "split" | "delete" | null>(null);
  const [sources, setSources] = React.useState<{ id: string; representative: { question_excerpt?: string; question?: string }; occurrence_count: number }[]>([]);
  const [sourceIds, setSourceIds] = React.useState<string[]>([]);
  const [mergeQuery, setMergeQuery] = React.useState("");
  const [mergeLoading, setMergeLoading] = React.useState(false);
  const [selection, setSelection] = React.useState<{ groupId: string; ids: string[] }>({ groupId, ids: [] });
  const currentEventIds = new Set(events.map((event) => event.id));
  const selectedEvents = selection.groupId === groupId
    ? selection.ids.filter((id) => currentEventIds.has(id))
    : [];
  const setSelectedEvents = (update: (ids: string[]) => string[]) => {
    setSelection((current) => ({
      groupId,
      ids: update(current.groupId === groupId ? current.ids : []),
    }));
  };
  const [splitRepresentative, setSplitRepresentative] = React.useState("");
  const [deleteEventId, setDeleteEventId] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [similar, setSimilar] = React.useState<{ group_id:string; question_excerpt:string; score:number }[] | null>(null);
  const triggerRef = React.useRef<HTMLElement | null>(null);
  const dialogRef = React.useRef<HTMLElement | null>(null);
  function openDialog(kind: "merge" | "split" | "delete", trigger: HTMLElement, eventId?: string, preselected: string[] = []) {
    triggerRef.current = trigger;
    if (eventId) setDeleteEventId(eventId);
    if (kind === "merge") {
      setSourceIds(preselected);
      setMergeQuery("");
      setMergeLoading(true);
    }
    setError("");
    setDialog(kind);
  }
  function closeDialog() {
    setDialog(null);
    requestAnimationFrame(() => triggerRef.current?.focus());
  }
  React.useEffect(() => {
    if (dialog) dialogRef.current?.focus();
  }, [dialog]);
  React.useEffect(() => {
    if (dialog !== "merge" || !group) return;
    let active = true;
    const query = new URLSearchParams({ project_id: group.project.id, event_type: group.event_type, limit: "50" });
    if (mergeQuery.trim()) query.set("q", mergeQuery.trim());
    apiRequest<{ items: typeof sources }>(`admin/groups?${query}`).then(page => {
      if (active) setSources(page.items.filter(item => item.id !== groupId));
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : "Could not load compatible groups."); })
      .finally(() => { if (active) setMergeLoading(false); });
    return () => { active = false; };
  }, [dialog, group, groupId, mergeQuery]);
  const reload = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [g, e, h, d] = await Promise.all([
        apiRequest<Group>(`admin/groups/${groupId}`),
        apiRequest<{ items: Event[]; next_cursor: string | null }>(
          `admin/groups/${groupId}/events`,
        ),
        apiRequest<{
          items: { id: string; action: string; created_at: string; actor_type?: string; from_status?: string | null; to_status?: string | null; details?: Record<string, unknown> }[];
        }>(`admin/groups/${groupId}/history`),
        apiRequest<DeliveryPage>(`deliveries?group_id=${groupId}&limit=50`),
      ]);
      setGroup(g);
      setEvents(e.items);
      setMore(e.next_cursor);
      setHistory(h.items);
      setDeliveries(d.items);
      setDeliveryCursor(d.next_cursor ?? null);
    } catch (x) {
      setError(x instanceof Error ? x.message : "Could not load group.");
    } finally {
      setLoading(false);
    }
  }, [groupId]);
  React.useEffect(() => {
    let active = true;
    Promise.resolve().then(() => {
      if (active) void reload();
    });
    return () => {
      active = false;
    };
  }, [reload]);
  React.useEffect(() => {
    const id = new URLSearchParams(location.search).get("highlight");
    if (id && !loading)
      document
        .getElementById(`event-${id}`)
        ?.scrollIntoView({ block: "center" });
  }, [loading, events]);
  async function mutate(path: string, method: string, body: unknown) {
    setNotice("");
    try {
      await apiRequest(path, { method, body: JSON.stringify(body) });
      await reload();
      setNotice(`Changes saved · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save changes.");
      return false;
    }
  }
  async function confirmAction() {
    if (!dialog || saving) return;
    setSaving(true);
    setError("");
    try {
      if (dialog === "merge") {
        await apiRequest(`admin/groups/${groupId}/merge`, { method: "POST", body: JSON.stringify({ source_group_ids: sourceIds }) });
        closeDialog();
        await reload();
        navigate(`/inbox/groups/${groupId}`);
      } else if (dialog === "split") {
        await apiRequest<{ group_id: string }>(`admin/groups/${groupId}/split`, { method: "POST", body: JSON.stringify({ event_ids: selectedEvents, ...(splitRepresentative ? { representative_event_id: splitRepresentative } : {}) }) }).then(result => {
          closeDialog();
          navigate(`/inbox/groups/${result.group_id}`);
        });
      } else {
        const result = await apiRequest<{ group_deleted: boolean }>(`admin/feedback/${deleteEventId}`, { method: "DELETE" });
        closeDialog();
        if (result.group_deleted) navigate("/inbox");
        else await reload();
      }
    } catch (e) {
      if (e instanceof ApiError && e.code === "feedback_busy") setError("This feedback is being delivered, try again shortly.");
      else setError(e instanceof Error ? e.message : "Could not complete this action.");
    } finally { setSaving(false); }
  }
  if (loading && !group)
    return (
      <main className="dashboard-main">
        <p role="status">Loading group…</p>
      </main>
    );
  if (error && !group)
    return (
      <main className="dashboard-main">
        <div role="alert" className="error-message">
          {error}
          <button className="quiet-button" onClick={() => void reload()}>
            Retry
          </button>
        </div>
      </main>
    );
  if (!group)
    return (
      <main className="dashboard-main">
        <p>Group not found.</p>
        <button className="quiet-button" onClick={() => navigate("/inbox")}>
          Back to inbox
        </button>
      </main>
    );
  return (
    <main className="dashboard-main">
      <section
        className="dashboard feedback-detail"
        aria-labelledby="group-title"
      >
        <button className="text-button" onClick={() => navigate(`/inbox${window.location.search}`)}>
          ← Group inbox
        </button>
        <div className="dashboard-heading">
          <div>
            <h1 id="group-title" tabIndex={-1} className={questionExpanded ? "group-question expanded" : "group-question"}>
              {group.representative.question || "Untitled feedback group"}
            </h1>
            <button className="text-button" aria-expanded={questionExpanded} onClick={() => setQuestionExpanded(value => !value)}>{questionExpanded ? "Show less" : "Show full question"}</button>
            <div className="group-chips"><span className="scope-chip">{group.project.name}</span><span className="scope-chip">{group.event_type.replaceAll("_", " ")}</span><span className={`status status-${group.status}`}>{statusLabels[group.status] ?? group.status}</span></div>
            <p className="subtle">{group.occurrence_count} {group.occurrence_count === 1 ? "occurrence" : "occurrences"}</p>
          </div>
        </div>
        {error && (
          <p role="alert" className="error-message">
            {error}
          </p>
        )}
        <div className="group-detail-columns"><div className="group-detail-main">
        <section aria-labelledby="occurrences-title">
          <h2 id="occurrences-title">Occurrences</h2>
          <p className="subtle">The representative occurrence supplies the text used as this group’s title.</p>
          {events.map((ev) => (
            <article className="panel" id={`event-${ev.id}`} key={ev.id}>
              <h3>Occurrence <code>{ev.id}</code></h3>
              {ev.match && <p className="occurrence-match"><span className="badge">{matchLabels[ev.match.method] ?? "Match"}</span>{ev.match.triggered_reopen && <span className="badge active">Reopened this group</span>}</p>}
              {ev.semantic && <dl className="occurrence-summary"><div><dt>Semantic outcome</dt><dd>{ev.semantic.outcome}{ev.semantic.error_category ? ` · ${ev.semantic.error_category}` : ''}</dd></div>{ev.semantic.confidence != null && <div><dt>Confidence</dt><dd>{Math.round(ev.semantic.confidence * 100)}%</dd></div>}{ev.semantic.reason && <div><dt>Model explanation (unverified)</dt><dd>{ev.semantic.reason}</dd></div>}</dl>}
              {group.representative.event_id === ev.id ? <span className="badge active" title="This occurrence supplies the text used as the group title">Representative · group title</span> : <button className="quiet-button" onClick={() => void mutate(`admin/groups/${groupId}`, "PATCH", { representative_event_id: ev.id })}>Make representative</button>}
              <label><input type="checkbox" aria-label={`Select occurrence ${ev.id} for split`} checked={selectedEvents.includes(ev.id)} onChange={e => setSelectedEvents(current => e.target.checked ? [...current, ev.id] : current.filter(id => id !== ev.id))} /> Select for split</label>
              <dl className="occurrence-summary">
                {Boolean(ev.agent_reported.question) && <div><dt>Question</dt><dd>{String(ev.agent_reported.question)}</dd></div>}
                <div><dt>Reported via</dt><dd>{String(ev.system.submitted_via ?? "unknown")}</dd></div>
                {Boolean(ev.system.token_name) && <div><dt>Token</dt><dd>{String(ev.system.token_name)}</dd></div>}
                <div><dt>Reported</dt><dd>{ev.created_at ? <time dateTime={ev.created_at}>{new Date(ev.created_at).toLocaleString()}</time> : "Time unavailable"}</dd></div>
                {Boolean(ev.agent_reported.agent_id) && <div><dt>Agent</dt><dd>{String(ev.agent_reported.agent_id)}</dd></div>}
                {Boolean(ev.agent_reported.previous_answer) && <div><dt>Previous answer</dt><dd>{String(ev.agent_reported.previous_answer)}</dd></div>}
                {Boolean(ev.agent_reported.context) && <div><dt>Context</dt><dd>{String(ev.agent_reported.context)}</dd></div>}
                {ev.event_type === "knowledge_correction" && Boolean(ev.agent_reported.correction ?? ev.submitted_correction) && <div><dt>Submitted correction (unverified)</dt><dd>{stringify(ev.agent_reported.correction ?? ev.submitted_correction)}</dd></div>}
              </dl>
              <details><summary>Technical details</summary><h4>System</h4>{safe(ev.system)}{Boolean(ev.agent_reported.metadata) && Object.keys(ev.agent_reported.metadata as object).length > 0 && <><h4>Metadata</h4>{safe(ev.agent_reported.metadata)}</>}</details>
              <details className="occurrence-more"><summary>More actions</summary><button className="danger-button" aria-label={`Delete occurrence ${ev.id}`} onClick={e => openDialog("delete", e.currentTarget, ev.id)}>Delete occurrence</button></details>
            </article>
          ))}
          {!events.length && <p>No occurrences found.</p>}
        </section>
        </div><aside className="group-detail-triage">
        <section className="panel" aria-labelledby="triage-title">
          <h2 id="triage-title">Triage</h2>
          <p role="status" aria-live="polite" className="inline-status">{notice}</p>
          <div className="action-row">
            <button className="quiet-button" onClick={e => openDialog("merge", e.currentTarget)}>Merge groups</button>
            <button className="quiet-button" disabled={!selectedEvents.length || selectedEvents.length >= group.occurrence_count} aria-describedby="split-hint" onClick={e => { setSplitRepresentative(""); openDialog("split", e.currentTarget); }}>Split selected occurrences ({selectedEvents.length})</button>
          </div>
          <section aria-labelledby="similar-title"><h3 id="similar-title">Similar groups</h3><button className="quiet-button" onClick={async () => { try { const r = await apiRequest<{items:{group_id:string;question_excerpt:string;score:number}[]}>(`admin/groups/${groupId}/similar`); setSimilar(r.items); } catch(err) { setError(err instanceof Error ? err.message : 'Could not load similar groups.'); } }}>Find similar groups</button>{similar && (similar.length ? <ul>{similar.map(item => <li key={item.group_id}>{item.question_excerpt} <button className="text-button" onClick={e => openDialog('merge', e.currentTarget, undefined, [item.group_id])}>Merge this group</button></li>)}</ul> : <p className="subtle">No similar groups found.</p>)}</section>
          <p className="inline-note" id="split-hint">{!selectedEvents.length ? "Select an occurrence to enable splitting." : selectedEvents.length >= group.occurrence_count ? "Keep at least one occurrence in this group." : "Selected occurrences will move to a new group."}</p>
          <label>
            Status
            <select
              aria-label="Group status"
              value={group.status}
              onChange={(e) =>
                void mutate(`admin/groups/${groupId}`, "PATCH", {
                  status: e.target.value,
                })
              }
            >
              {["open", "in_progress", "resolved", "dismissed"].map((x) => (
                <option key={x} value={x}>{statusLabels[x]}</option>
              ))}
            </select>
          </label>
          <section aria-labelledby="admin-notes-title">
          <h3 id="admin-notes-title">Administrator notes</h3>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void mutate(`admin/groups/${groupId}/notes`, "POST", {
                body: note,
              }).then(saved => { if (saved) setNote(""); });
            }}
          >
            <label htmlFor="group-note">Administrator note</label>
            <textarea
              id="group-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={4000}
            />
            <p className="inline-note" id="note-visibility">Visible to administrators only.{!note.trim() && " Type a note first to enable Add note."}</p>
            <button className="primary-button" disabled={!note.trim()} aria-disabled={!note.trim()} aria-describedby="note-visibility">Add note</button>
          </form>
          <ul>
            {group.notes.map((n) => (
              <li key={n.id}>
                {safe(n.body)}
                {deleteNoteId === n.id ? <span className="inline-confirm"><span>Delete this note?</span><button className="danger-button" onClick={() => { setDeleteNoteId(null); void mutate(`admin/groups/${groupId}/notes/${n.id}`, "DELETE", {}); }}>Delete</button><button className="quiet-button" onClick={() => setDeleteNoteId(null)}>Keep</button></span> : <button
                  className="danger-button"
                  onClick={() => setDeleteNoteId(n.id)}
                >
                  Delete note
                </button>}
              </li>
            ))}
          </ul>
          </section>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void mutate(`admin/groups/${groupId}/tags`, "PATCH", {
                add: [tag],
              }).then(saved => { if (saved) setTag(""); });
            }}
          >
            <label htmlFor="group-tag">Add tag</label>
            <input
              id="group-tag"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
            />
            <button className="primary-button" disabled={!tag.trim()} aria-disabled={!tag.trim()} aria-describedby="tag-hint">Add tag</button>
          </form>
          <p className="inline-note" id="tag-hint">
            Tag edits do not re-route existing feedback.{!tag.trim() && " Enter a tag first to enable Add tag."}
          </p>
          <div>
            {group.tags.map((t) => (
              <span className="scope-chip" key={t.slug}>
                {t.name}{" "}
                <button
                  className="text-button tag-remove"
                  aria-label={`Remove tag ${t.name}`}
                  onClick={() =>
                    void mutate(`admin/groups/${groupId}/tags`, "PATCH", {
                      remove: [t.slug],
                    })
                  }
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </section>
        </aside></div>
        {more && <div className="group-detail-main"><button className="quiet-button" onClick={async () => {
          try {
            const page = await apiRequest<{ items: Event[]; next_cursor: string | null }>(`admin/groups/${groupId}/events?cursor=${encodeURIComponent(more)}`);
            setEvents(current => [...current, ...page.items]);
            setMore(page.next_cursor);
          } catch (e) { setError(e instanceof Error ? e.message : "Could not load occurrences."); }
        }}>Load more occurrences</button></div>}
        <section>
          <h2>Group history</h2>
          {history.map((h) => (
            <p key={h.id}>
              {h.action}{h.from_status || h.to_status ? ` · ${h.from_status ?? "∅"} → ${h.to_status ?? "∅"}` : ""} · {h.actor_type ?? "unknown actor"} · {new Date(h.created_at).toLocaleString()}
              {h.action === "merged" && <span> · {Array.isArray(h.details?.source_group_ids) ? h.details.source_group_ids.length : 0} source groups · {typeof h.details?.moved_events === "number" ? h.details.moved_events : 0} occurrences</span>}
              {(h.action === "split_out" || h.action === "split_in") && <span> · {Array.isArray(h.details?.event_ids) ? h.details.event_ids.length : 0} occurrences</span>}
            </p>
          ))}
        </section>
        <section>
          <h2>Delivery history</h2>
          {!deliveries.length && <div className="empty-card"><p>No deliveries for this group yet. Deliveries appear when a routing rule matches.</p><button className="text-button" onClick={() => navigate(`/?project=${encodeURIComponent(group.project.id)}#routing`)}>Open routing</button></div>}
          {deliveries.map((d) => (
            <article className="panel" key={d.id}>
              <p>
                {d.status} · {d.attempt_count} attempts · event {d.event_id}
              </p>
              <button
                className="quiet-button"
                onClick={async () => {
                  try {
                    setAttempts(await apiRequest(`deliveries/${d.id}`));
                  } catch (e) {
                    setError(
                      e instanceof Error
                        ? e.message
                        : "Could not load attempts.",
                    );
                  }
                }}
              >
                View attempts
              </button>
              {d.status === "failed" && <p><strong>Failure reason:</strong> {d.last_error_message || "No failure reason recorded."}</p>}
              {d.status === "failed" && (
                <button
                  onClick={() =>
                    void mutate(`deliveries/${d.id}/retry`, "POST", {})
                  }
                >
                  Retry
                </button>
              )}
            </article>
          ))}
          {deliveryCursor && <button className="quiet-button" disabled={deliveryLoading} onClick={async () => {
            setDeliveryLoading(true);
            try {
              const page = await apiRequest<DeliveryPage>(`deliveries?group_id=${groupId}&limit=50&cursor=${encodeURIComponent(deliveryCursor)}`);
              setDeliveries(current => { const ids = new Set(current.map(item => item.id)); return [...current, ...page.items.filter(item => !ids.has(item.id))]; });
              setDeliveryCursor(page.next_cursor);
            } catch (e) { setError(e instanceof Error ? e.message : "Could not load deliveries."); }
            finally { setDeliveryLoading(false); }
          }}>{deliveryLoading ? "Loading…" : "Load more deliveries"}</button>}
        </section>
        {attempts && (
          <div
            className="dialog-backdrop"
            role="presentation"
            onClick={() => setAttempts(null)}
          >
            <section
              className="confirm-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="attempt-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="attempt-title">Delivery attempts</h2>
              {safe(attempts)}
              <button onClick={() => setAttempts(null)}>Close</button>
            </section>
          </div>
        )}
        {dialog && (
          <div className="dialog-backdrop" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) closeDialog(); }}>
            <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="action-dialog-title" tabIndex={-1} ref={dialogRef} onKeyDown={e => {
              if (e.key === "Escape" && !saving) { e.stopPropagation(); closeDialog(); }
              if (e.key === "Tab") {
                const focusable = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]'));
                const first = focusable[0], last = focusable[focusable.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
              }
            }}>
              {dialog === "merge" ? <>
                <h2 id="action-dialog-title">Merge groups into this group</h2>
                <label htmlFor="merge-search">Search compatible groups</label>
                <input id="merge-search" type="search" value={mergeQuery} onChange={e => setMergeQuery(e.target.value)} />
                {mergeLoading ? <p role="status">Loading compatible groups…</p> : sources.length ? <fieldset><legend>Select source groups</legend>{sources.map(source => <label key={source.id}><input type="checkbox" checked={sourceIds.includes(source.id)} onChange={e => setSourceIds(ids => e.target.checked ? [...ids, source.id] : ids.filter(id => id !== source.id))} />{source.representative.question_excerpt ?? source.representative.question ?? source.id} ({source.occurrence_count})</label>)}</fieldset> : <p>No compatible groups found.</p>}
                {group.status === "resolved" || group.status === "dismissed" ? <p className="inline-note">This target is {group.status}; it will keep that status after the merge.</p> : null}
                <p>Selected groups and their occurrences, notes and history will be moved into this group.</p>
              </> : dialog === "split" ? <>
                <h2 id="action-dialog-title">Split selected occurrences</h2>
                <p>Selected occurrences move into a new open group. At least one occurrence must remain here.</p>
                {selectedEvents.length >= group.occurrence_count && <p role="alert">Select fewer than all occurrences to split.</p>}
                <label htmlFor="split-representative">Representative for the new group (optional)</label>
                <select id="split-representative" value={splitRepresentative} onChange={e => setSplitRepresentative(e.target.value)}><option value="">First selected occurrence</option>{events.filter(ev => selectedEvents.includes(ev.id)).map(ev => <option key={ev.id} value={ev.id}>{ev.id}</option>)}</select>
              </> : <>
                <h2 id="action-dialog-title">Delete this occurrence?</h2>
                <p>Its delivery records will be removed. Copies already sent to destinations cannot be recalled.</p>
                {group.occurrence_count === 1 && <p role="alert">This is the last occurrence. The group and its notes will also be deleted, and you will return to the inbox.</p>}
              </>}
              {error && <p role="alert" className="error-message">{error}</p>}
              <div className="action-row"><button className="quiet-button" onClick={closeDialog} disabled={saving}>Cancel</button><button className={dialog === "delete" ? "danger-button" : ""} onClick={() => void confirmAction()} disabled={saving || (dialog === "merge" && sourceIds.length === 0) || (dialog === "split" && (!selectedEvents.length || selectedEvents.length >= group.occurrence_count))}>{saving ? "Working…" : dialog === "merge" ? "Confirm merge" : dialog === "split" ? "Confirm split" : "Delete occurrence"}</button></div>
            </section>
          </div>
        )}
      </section>
    </main>
  );
}
