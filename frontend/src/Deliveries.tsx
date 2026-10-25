import React from "react";
import { apiRequest } from "./api";
type Delivery = {
  id: string;
  event_id: string;
  project_id: string;
  destination_id: string;
  status: string;
  attempt_count: number;
  last_error_category: string | null;
  next_attempt_at: string | null;
  created_at: string;
};
type Detail = Delivery & {
  attempts: {
    attempt_number: number;
    trigger: string;
    started_at: string;
    completed_at: string | null;
    duration_ms: number | null;
    result: string;
    http_status: number | null;
    error_category: string | null;
    sanitized_error: string | null;
  }[];
};
type Option = { id: string; name: string; enabled?: boolean };
const date = (v: string | null) => (v ? new Date(v).toLocaleString() : "—");
const deliveryStatus = (status: string) => status === "suppressed_duplicate" ? "Duplicate suppressed · already sent for this group" : status.replaceAll("_", " ");
export function Deliveries({ navigate }: { navigate: (url: string) => void }) {
  const params = new URLSearchParams(location.search);
  const status = params.get("status") ?? "",
    destination = params.get("destination_id") ?? "",
    project = params.get("project_id") ?? "";
  const [items, setItems] = React.useState<Delivery[]>([]),
    [cursor, setCursor] = React.useState<string | null>(null),
    [destinations, setDestinations] = React.useState<Option[]>([]),
    [projects, setProjects] = React.useState<Option[]>([]),
    [detail, setDetail] = React.useState<Detail | null>(null),
    [error, setError] = React.useState(""),
    [loading, setLoading] = React.useState(true),
    [retryBusy, setRetryBusy] = React.useState(false),
    [refresh, setRefresh] = React.useState(0),
    [retryNotice, setRetryNotice] = React.useState("");
  React.useEffect(() => {
    apiRequest<{ items: Option[] }>("destinations")
      .then((x) => setDestinations(x.items))
      .catch(() => {});
    apiRequest<{ items: Option[] }>("projects?limit=100")
      .then((x) => setProjects(x.items))
      .catch(() => {});
  }, []);
  React.useEffect(() => {
    let active = true;
    Promise.resolve().then(() => {
      if (active) {
        setLoading(true);
        setError("");
      }
    });
    const deliveryId = new URLSearchParams(location.search).get("delivery_id");
    if (deliveryId)
      apiRequest<Detail>(`deliveries/${deliveryId}`)
        .then((x) => {
          if (active) setDetail(x);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    const q = new URLSearchParams({ limit: "30" });
    if (status) q.set("status", status);
    if (destination) q.set("destination_id", destination);
    if (project) q.set("project_id", project);
    apiRequest<{ items: Delivery[]; next_cursor: string | null }>(
      `deliveries?${q}`,
    )
      .then((x) => {
        if (active) {
          setItems(x.items);
          setCursor(x.next_cursor);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [status, destination, project, refresh]);
  function filter(k: string, v: string) {
    const q = new URLSearchParams(location.search);
    if (v) q.set(k, v);
    else q.delete(k);
    q.delete("delivery_id");
    navigate(`/deliveries${q.size ? `?${q}` : ""}`);
  }
  async function open(item: Delivery) {
    setError("");
    try {
      setDetail(await apiRequest<Detail>(`deliveries/${item.id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load delivery.");
    }
  }
  async function retryDelivery() {
    if (!detail) return;
    setRetryBusy(true);
    try {
      const updated = await apiRequest<Detail>(
        `deliveries/${detail.id}/retry`,
        { method: "POST", body: "{}" },
      );
      setDetail(updated);
      setItems((rows) => rows.map((x) => (x.id === updated.id ? updated : x)));
      setRetryNotice(`Retry finished: ${updated.status}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Retry failed.");
    } finally {
      setRetryBusy(false);
    }
  }
  return (
    <main className="dashboard-main">
      <section className="dashboard">
        <div className="dashboard-heading">
          <div>
            <div className="eyebrow">DELIVERY</div>
            <h1 tabIndex={-1}>Delivery log</h1>
            <p className="subtle">
              Inspect delivery attempts and retry eligible failures.
            </p>
          </div>
        </div>
        <div role="status" aria-live="polite" className="notice">
          {retryBusy ? "Retrying delivery…" : retryNotice}
        </div>
        {error && (
          <div role="alert" className="error-message">
            {error}{" "}
            <button
              className="quiet-button"
              onClick={() =>
                detail
                  ? void open(items.find((x) => x.id === detail.id)!)
                  : setRefresh((x) => x + 1)
              }
            >
              Retry
            </button>
          </div>
        )}
        {detail && (
          <section className="panel">
            <div className="panel-heading">
              <h2>Delivery details</h2>
              <button className="quiet-button" onClick={() => setDetail(null)}>
                Back to log
              </button>
            </div>
            <p>
              Status: <span className="badge">{deliveryStatus(detail.status)}</span> · Attempts:{" "}
              {detail.attempt_count}
            </p>
            {detail.status === "failed" &&
              destinations.find((d) => d.id === detail.destination_id)
                ?.enabled === true && (
                <button
                  disabled={retryBusy}
                  onClick={() => void retryDelivery()}
                >
                  {retryBusy ? "Retrying…" : "Retry"}
                </button>
              )}
            <h3>Attempts</h3>
            {detail.attempts.length ? (
              <>{detail.attempt_count > detail.attempts.length && <p className="inline-note">{detail.attempts.length ? `Attempts 1–${Math.min(...detail.attempts.map(a=>a.attempt_number))-1} removed by retention.` : 'Attempt details removed by retention. This delivery finished more than 30 days ago. Its status and attempt count are kept.'}</p>}{detail.attempts.map((a) => (
                <article className="attempt-card" key={a.attempt_number}>
                  <h4>Attempt {a.attempt_number}</h4>
                  <p>
                    Trigger: {a.trigger} · Started: {date(a.started_at)} ·
                    Duration: {a.duration_ms ?? "—"} ms
                  </p>
                  <p>
                    Result: {a.result} · HTTP: {a.http_status ?? "—"} ·
                    Category: {a.error_category ?? "—"}
                  </p>
                  <p>{a.sanitized_error ?? "—"}</p>
                </article>
              ))}</>
            ) : (
              <p className="inline-note">{detail.attempt_count > 0 ? 'Attempt details removed by retention. Its status and attempt count are kept.' : 'No attempts recorded.'}</p>
            )}
          </section>
        )}
        <section className="panel">
          <div className="inbox-filters">
            <label>
              State
              <select
                aria-label="Filter by state"
                value={status}
                onChange={(e) => filter("status", e.target.value)}
              >
                <option value="">All states</option>
                {[
                  "pending",
                  "processing",
                  "retry_scheduled",
                  "delivered",
                  "failed",
                ].map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </label>
            <label>
              Destination
              <select
                aria-label="Filter by destination"
                value={destination}
                onChange={(e) => filter("destination_id", e.target.value)}
              >
                <option value="">All destinations</option>
                {destinations.map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Project
              <select
                aria-label="Filter by project"
                value={project}
                onChange={(e) => filter("project_id", e.target.value)}
              >
                <option value="">All projects</option>
                {projects.map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {loading ? (
            <p role="status">Loading deliveries…</p>
          ) : error ? null : items.length === 0 ? (
            <div className="empty-state">
              <h2>No deliveries yet</h2>
              <p>A delivery is created when a routing rule matches an event.</p>
              <button className="quiet-button" onClick={() => navigate('/destinations')}>Open routing</button>
            </div>
          ) : (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Project</th>
                      <th>Event</th>
                      <th>Destination</th>
                      <th>State</th>
                      <th>Attempts</th>
                      <th>Last error</th>
                      <th>Next attempt</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((x) => (
                      <tr key={x.id}>
                        <td>{date(x.created_at)}</td>
                        <td>
                          {projects.find((p) => p.id === x.project_id)?.name ??
                            x.project_id}
                        </td>
                        <td>
                          <a
                            href={`/inbox/${x.event_id}`}
                            onClick={(e) => {
                              e.preventDefault();
                              navigate(`/inbox/${x.event_id}`);
                            }}
                          >
                            View event
                          </a>
                        </td>
                        <td>
                          {destinations.find((d) => d.id === x.destination_id)
                            ?.name ?? x.destination_id}
                        </td>
                        <td>
                          <span className="badge">{deliveryStatus(x.status)}</span>
                        </td>
                        <td>{x.attempt_count}</td>
                        <td>{x.last_error_category ?? "—"}</td>
                        <td>{date(x.next_attempt_at)}</td>
                        <td>
                          <button
                            className="text-button"
                            onClick={() => void open(x)}
                          >
                            Details
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {cursor && (
                <button
                  className="quiet-button"
                  onClick={() => {
                    const q = new URLSearchParams(location.search);
                    q.set("limit", "30");
                    q.set("cursor", cursor);
                    apiRequest<{
                      items: Delivery[];
                      next_cursor: string | null;
                    }>(`deliveries?${q}`)
                      .then((r) => {
                        setItems((x) => [...x, ...r.items]);
                        setCursor(r.next_cursor);
                      })
                      .catch((e) => setError(e.message));
                  }}
                >
                  Load more
                </button>
              )}
            </>
          )}
        </section>
      </section>
    </main>
  );
}
