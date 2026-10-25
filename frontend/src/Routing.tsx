import React from "react";
import { apiRequest } from "./api";

type Destination = { id: string; name: string; enabled: boolean };
type Rule = {
  id: string;
  name: string;
  enabled: boolean;
  event_types: string[];
  match_mode: "any" | "all";
  tags: string[];
  priority: number;
  destination_ids: string[];
  match_behavior: "continue" | "stop";
  created_at: string;
};
type Project = {
  id: string;
  tags: { name: string; slug: string }[];
  fallback_destination_id?: string | null;
};
type Simulation = {
  evaluated_rules: { rule_id: string; status: string; reason: string }[];
  stopped_by_rule_id: string | null;
  destinations: {
    destination_id: string;
    via: string;
    outcome: string;
    rule_id: string | null;
  }[];
  fallback_applied: boolean;
  warnings: string[];
};
const normalize = (value: string) =>
  value
    .trim()
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-|-$/g, "");
export function Routing({ project }: { project: Project }) {
  const [rules, setRules] = React.useState<Rule[]>([]),
    [destinations, setDestinations] = React.useState<Destination[]>([]),
    [fallback, setFallback] = React.useState(
      project.fallback_destination_id ?? "",
    ),
    [editing, setEditing] = React.useState<Rule | true | false>(false),
    [tags, setTags] = React.useState(""),
    [sim, setSim] = React.useState<Simulation | null>(null),
    [error, setError] = React.useState(""),
    [busy, setBusy] = React.useState(false),
    [eventType, setEventType] = React.useState("knowledge_gap"),
    [simTags, setSimTags] = React.useState(""),
    [includeDefaults, setIncludeDefaults] = React.useState(true);
  const load = React.useCallback(async () => {
    const [r, d, p] = await Promise.all([
      apiRequest<{ items: Rule[] }>(`projects/${project.id}/routing-rules`),
      apiRequest<{ items: Destination[] }>("destinations"),
      apiRequest<Project>(`projects/${project.id}`),
    ]);
    setRules(r.items ?? []);
    setDestinations(d.items ?? []);
    setFallback(p.fallback_destination_id ?? "");
  }, [project.id]);
  React.useEffect(() => {
    Promise.resolve()
      .then(() => load())
      .catch((e) => setError(e.message));
  }, [load]);
  async function save(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const selectedDestinations = (
      e.currentTarget.elements.namedItem("destinations") as HTMLSelectElement
    ).selectedOptions;
    if (
      !["knowledge_gap", "knowledge_correction"].some((x) => f.get(x) === "on")
    ) {
      setError("Choose at least one event type.");
      return;
    }
    if (
      tags
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean).length > 20
    ) {
      setError("Use no more than 20 tags.");
      return;
    }
    if (selectedDestinations.length < 1 || selectedDestinations.length > 10) {
      setError("Choose between 1 and 10 destinations.");
      return;
    }
    const body = {
      name: String(f.get("name")).trim(),
      enabled: f.get("enabled") === "on",
      event_types: ["knowledge_gap", "knowledge_correction"].filter(
        (x) => f.get(x) === "on",
      ),
      match_mode: String(f.get("match_mode")),
      tags: tags
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
      priority: Number(f.get("priority")),
      destination_ids: Array.from(selectedDestinations).map((x) => x.value),
      match_behavior: String(f.get("match_behavior")),
    };
    setBusy(true);
    setError("");
    try {
      await apiRequest(
        `projects/${project.id}/routing-rules${editing && editing !== true ? `/${editing.id}` : ""}`,
        {
          method: editing && editing !== true ? "PATCH" : "POST",
          body: JSON.stringify(body),
        },
      );
      setEditing(false);
      await load();
    } catch (x) {
      setError(x instanceof Error ? x.message : "Could not save rule.");
    } finally {
      setBusy(false);
    }
  }
  async function setFallbackValue(value: string) {
    const previous = fallback;
    setFallback(value);
    try {
      await apiRequest(`projects/${project.id}`, {
        method: "PATCH",
        body: JSON.stringify({ fallback_destination_id: value || null }),
      });
    } catch (e) {
      setFallback(previous);
      setError(e instanceof Error ? e.message : "Could not save fallback.");
    }
  }
  async function simulate(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const result = await apiRequest<Simulation>(
        `projects/${project.id}/routing/simulate`,
        {
          method: "POST",
          body: JSON.stringify({
            event_type: eventType,
            tags: simTags
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean),
            include_default_tags: includeDefaults,
          }),
        },
      );
      setSim(result);
    } catch (x) {
      setError(x instanceof Error ? x.message : "Simulation failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel routing-panel" aria-labelledby="routing-title">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">ROUTING</div>
          <h2 id="routing-title">Routing rules</h2>
        </div>
        <button
          onClick={() => {
            setTags("");
            setEditing(true);
          }}
        >
          Add rule
        </button>
      </div>
      {error && (
        <p role="alert" className="error-message">
          {error}
        </p>
      )}
      {rules.length === 0 ? (
        <p className="empty-inline">No routing rules yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Rule</th>
                <th>Events and tags</th>
                <th>Destinations</th>
                <th>Action</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{r.priority}</td>
                  <th>
                    {r.name}
                    <small>{r.enabled ? "Enabled" : "Disabled"}</small>
                  </th>
                  <td>
                    {r.event_types.join(", ")} · {r.match_mode.toUpperCase()}{" "}
                    {r.tags.map((t) => (
                      <span className="scope-chip" key={t}>
                        {t}
                      </span>
                    ))}
                  </td>
                  <td>
                    {r.destination_ids
                      .map(
                        (id) =>
                          destinations.find((d) => d.id === id)?.name ??
                          "Unknown",
                      )
                      .join(", ")}
                  </td>
                  <td>{r.match_behavior}</td>
                  <td>
                    <button
                      className="text-button"
                      onClick={() => {
                        setTags(r.tags.join(", "));
                        setEditing(r);
                      }}
                    >
                      Edit
                    </button>{" "}
                    <button
                      className="text-button"
                      onClick={() => {
                        if (window.confirm(`Delete “${r.name}”?`))
                          void apiRequest(
                            `projects/${project.id}/routing-rules/${r.id}`,
                            { method: "DELETE" },
                          )
                            .then(load)
                            .catch((e) => setError(e.message));
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="routing-options">
        <label>
          Fallback destination
          <select
            aria-label="Fallback destination"
            value={fallback}
            onChange={(e) => void setFallbackValue(e.target.value)}
          >
            <option value="">None</option>
            {destinations.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.enabled ? "" : " (disabled)"}
              </option>
            ))}
          </select>
        </label>
        <p className="help-text">
          Used only when no enabled rule matches. Never used after a failed
          delivery. Events always stay in the inbox.
        </p>
      </div>
      {editing !== false && (
        <form className="routing-form" onSubmit={save}>
          <h3>{editing && editing !== true ? "Edit rule" : "Create rule"}</h3>
          <label>
            Name
            <input
              name="name"
              required
              maxLength={120}
              defaultValue={editing && editing !== true ? editing.name : ""}
            />
          </label>
          <label className="checkbox-row">
            <input
              name="enabled"
              type="checkbox"
              defaultChecked={
                editing && editing !== true ? editing.enabled : true
              }
            />{" "}
            Enabled
          </label>
          <fieldset>
            <legend>Event types (choose at least one)</legend>
            {["knowledge_gap", "knowledge_correction"].map((x) => (
              <label className="checkbox-row" key={x}>
                <input
                  type="checkbox"
                  name={x}
                  defaultChecked={
                    editing && editing !== true
                      ? editing.event_types.includes(x)
                      : x === "knowledge_gap"
                  }
                />{" "}
                {x.replace("_", " ")}
              </label>
            ))}
          </fieldset>
          <label>
            Match mode
            <select
              name="match_mode"
              defaultValue={
                editing && editing !== true ? editing.match_mode : "any"
              }
            >
              <option value="any">ANY</option>
              <option value="all">ALL</option>
            </select>
          </label>
          <p className="help-text">
            ANY matches one or more tags; ALL requires every listed tag. No tags
            matches every tag set.
          </p>
          <label>
            Tags (comma separated)
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              aria-describedby="tag-preview"
            />
          </label>
          <p id="tag-preview" className="help-text">
            Normalized preview:{" "}
            {tags.split(",").map(normalize).filter(Boolean).join(", ") ||
              "No tags"}{" "}
            · up to 20. Suggestions:{" "}
            {(project.tags ?? []).map((t) => t.name).join(", ") || "none"}
          </p>
          <label>
            Priority (lower runs first)
            <input
              name="priority"
              type="number"
              min="0"
              max="1000000"
              defaultValue={
                editing && editing !== true ? editing.priority : 100
              }
              required
            />
          </label>
          <label>
            Destinations (1–10)
            <select
              name="destinations"
              multiple
              required
              defaultValue={
                editing && editing !== true ? editing.destination_ids : []
              }
              size={Math.min(5, Math.max(2, destinations.length))}
            >
              {destinations.map((d) => (
                <option key={d.id} value={d.id} disabled={!d.enabled}>
                  {d.name}
                  {d.enabled ? "" : " (disabled)"}
                </option>
              ))}
            </select>
          </label>
          <label>
            When matched
            <select
              name="match_behavior"
              defaultValue={
                editing && editing !== true
                  ? editing.match_behavior
                  : "continue"
              }
            >
              <option value="continue">Continue to next rule</option>
              <option value="stop">Stop evaluation</option>
            </select>
          </label>
          <p className="help-text">
            Stop prevents later rules from running after this rule matches.
          </p>
          <div className="button-row">
            <button disabled={busy}>Save rule</button>
            <button
              type="button"
              className="quiet-button"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      <section className="simulator" aria-labelledby="sim-title">
        <h3 id="sim-title">Routing simulator</h3>
        <p>
          <strong>Dry run: nothing is sent.</strong>
        </p>
        <form onSubmit={simulate}>
          <label>
            Event type
            <select
              value={eventType}
              onChange={(e) => setEventType(e.target.value)}
            >
              <option value="knowledge_gap">Knowledge gap</option>
              <option value="knowledge_correction">Knowledge correction</option>
            </select>
          </label>
          <label>
            Tags (comma separated)
            <input
              value={simTags}
              onChange={(e) => setSimTags(e.target.value)}
            />
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={includeDefaults}
              onChange={(e) => setIncludeDefaults(e.target.checked)}
            />{" "}
            Include project default tags
          </label>
          <button disabled={busy}>Simulate</button>
        </form>
        {sim && (
          <div aria-live="polite" className="simulation-result">
            <h4>Evaluation</h4>
            <ul>
              {sim.evaluated_rules.map((r, i) => (
                <li key={r.rule_id}>
                  <strong>
                    {rules.find((x) => x.id === r.rule_id)?.name ??
                      `Rule ${i + 1}`}
                    :
                  </strong>{" "}
                  {r.status === "skipped_disabled"
                    ? "disabled"
                    : r.status === "not_evaluated_stopped"
                      ? "not evaluated because a stop rule ended evaluation"
                    : r.status === "not_matched"
                        ? "not matched"
                      : r.status === "suppressed_duplicate"
                        ? "duplicate suppressed (already sent for this group)"
                        : "matched"}{" "}
                  — {r.reason}{" "}
                  {r.rule_id === sim.stopped_by_rule_id && <b>Stop rule</b>}
                </li>
              ))}
            </ul>
            <p>
              Destinations:{" "}
              {sim.destinations
                .filter((d) => d.outcome === "delivery_created")
                .map(
                  (d) =>
                    `${destinations.find((x) => x.id === d.destination_id)?.name ?? d.destination_id} (${d.via})`,
                )
                .join(", ") || "None"}
            </p>
            {sim.destinations.filter((d) => d.outcome !== "delivery_created")
              .length > 0 && (
              <ul aria-label="Skipped destinations">
                {sim.destinations
                  .filter((d) => d.outcome !== "delivery_created")
                  .map((d) => (
                    <li key={`${d.destination_id}-${d.via}-${d.outcome}`}>
                      Skipped:{" "}
                      {destinations.find((x) => x.id === d.destination_id)
                        ?.name ?? d.destination_id}{" "}
                      —{" "}
                      {d.outcome === "duplicate_destination"
                        ? "duplicate"
                        : d.outcome === "destination_disabled" ||
                            d.outcome === "fallback_skipped_disabled"
                          ? "disabled"
                          : d.outcome}
                    </li>
                  ))}
              </ul>
            )}
            <p>Fallback applied: {sim.fallback_applied ? "Yes" : "No"}</p>
            {sim.warnings.length > 0 && (
              <ul>
                {sim.warnings.map((w) => (
                  <li key={w}>Warning: {w}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>
    </section>
  );
}
