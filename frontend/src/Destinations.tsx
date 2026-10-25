import React from "react";
import { ApiError, apiRequest } from "./api";
type SecretStatus = { set: boolean };
type DestinationConfig = {
  url_origin?: string;
  url_has_path?: boolean;
  auth?: {
    type?: string;
    name?: string;
    token?: SecretStatus;
    value?: SecretStatus;
  };
  signing?: boolean;
  custom_headers?: string[];
  timeout_seconds?: number;
  host?: string;
  port?: number;
  tls_mode?: string;
  username?: string | SecretStatus;
  password?: SecretStatus;
  from?: string;
  to?: string[];
  [key: string]: unknown;
};
type WebhookConfig = DestinationConfig & {
  auth?: NonNullable<DestinationConfig["auth"]>;
  custom_headers?: string[];
};
type SmtpConfig = DestinationConfig & {
  host?: string;
  port?: number;
  tls_mode?: string;
  username?: string | SecretStatus;
  password?: SecretStatus;
  from?: string;
  to?: string[];
};
type InboxConfig = DestinationConfig;
type ConfigUpdate = Record<string, unknown>;

type DestinationBase = {
  id: string;
  name: string;
  enabled: boolean;
  delivery_mode: string;
  allow_private_network: boolean;
  is_system: boolean;
  signing_secret?: string;
};
type Destination =
  | (DestinationBase & { type: "webhook"; config: WebhookConfig })
  | (DestinationBase & { type: "smtp"; config: SmtpConfig })
  | (DestinationBase & { type: "inbox"; config: InboxConfig });
type FormData = {
  name: string;
  type: string;
  enabled: boolean;
  delivery_mode: string;
  allow_private_network: boolean;
  config: ConfigUpdate;
  secrets: Record<string, string>;
};
const modeLabel = (mode: string) =>
  mode === "every_occurrence" ? "Every occurrence" : "New groups only";

export function Destinations() {
  const [items, setItems] = React.useState<Destination[]>([]);
  const [selected, setSelected] = React.useState<Destination | null>(null);
  const [creating, setCreating] = React.useState(false);
  const [kind, setKind] = React.useState<"webhook" | "smtp">("webhook");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [notice, setNotice] = React.useState("");
  const [onceSecret, setOnceSecret] = React.useState("");
  const [privateConfirm, setPrivateConfirm] = React.useState(false);
  const [pendingPrivate, setPendingPrivate] = React.useState(false);
  const [tlsNone, setTlsNone] = React.useState(false);
  const formRef = React.useRef<HTMLFormElement>(null);
  const previousFocus = React.useRef<HTMLElement | null>(null);
  const dialogRef = React.useRef<HTMLElement>(null);
  React.useEffect(() => {
    if (privateConfirm)
      dialogRef.current?.querySelector<HTMLElement>("button")?.focus();
  }, [privateConfirm]);
  const refresh = React.useCallback(async () => {
    const result = await apiRequest<{ items: Destination[] }>("destinations");
    setItems(result.items);
  }, []);
  React.useEffect(() => {
    let live = true;
    apiRequest<{ items: Destination[] }>("destinations")
      .then((result) => {
        if (live) setItems(result.items);
      })
      .catch((e: unknown) => {
        if (live) setError(message(e));
      });
    return () => {
      live = false;
    };
  }, []);
  function startCreate(type: "webhook" | "smtp") {
    setPendingPrivate(false);
    setSelected(null);
    setCreating(true);
    setKind(type);
    setTlsNone(false);
    setFields({});
    setError("");
    setNotice("");
    setOnceSecret("");
  }
  async function edit(item: Destination) {
    setPendingPrivate(false);
    setError("");
    setFields({});
    try {
      const fresh = await apiRequest<Destination>(`destinations/${item.id}`);
      setSelected(fresh);
      setCreating(true);
      setKind(fresh.type as "webhook" | "smtp");
      setTlsNone(fresh.config.tls_mode === "none");
      setNotice("");
      setOnceSecret("");
    } catch (e) {
      setError(message(e));
    }
  }
  function parseForm(
    form: HTMLFormElement,
    type: "webhook" | "smtp",
  ): FormData {
    const d = new window.FormData(form);
    const get = (key: string) => String(d.get(key) ?? "");
    const config: ConfigUpdate = {
      timeout_seconds: Number(get("timeout_seconds")),
    };
    const secrets: Record<string, string> = {};
    if (type === "webhook") {
      const auth = get("auth");
      config.auth =
        auth === "bearer"
          ? { type: "bearer" }
          : auth === "header"
            ? { type: "header", name: get("header_name") }
            : { type: "none" };
      config.signing = d.has("signing");
      const headers = get("custom_headers")
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean)
        .map((line) => {
          const ix = line.indexOf(":");
          return ix < 0
            ? [line, ""]
            : [line.slice(0, ix).trim(), line.slice(ix + 1).trim()];
        });
      const customHeaders: Record<string, string | null> = Object.fromEntries(
        headers.filter(([, value]) => value),
      );
      for (const name of d.getAll("clear_custom_header"))
        customHeaders[name as string] = null;
      config.custom_headers = customHeaders;
      if (get("url")) secrets.url = get("url");
      if (auth === "bearer") {
        if (get("token")) secrets.token = get("token");
      }
      if (auth === "header") {
        if (get("header_value")) secrets.header_value = get("header_value");
      }
    } else {
      config.host = get("host");
      config.port = Number(get("port"));
      config.tls_mode = get("tls_mode");
      config.from = get("from");
      config.to = get("to")
        .split(/[\n,]/)
        .map((v) => v.trim())
        .filter(Boolean);
      if (get("tls_mode") !== "none" && get("username"))
        config.username = get("username");
      if (get("tls_mode") === "none" && d.has("clear_username"))
        config.username = null;
      if (get("tls_mode") !== "none" && get("password"))
        secrets.password = get("password");
    }
    return {
      name: get("name"),
      type,
      enabled: d.has("enabled"),
      delivery_mode: get("delivery_mode"),
      allow_private_network: d.has("allow_private_network"),
      config,
      secrets,
    };
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !selected &&
      (
        formRef.current?.elements.namedItem(
          "allow_private_network",
        ) as HTMLInputElement
      )?.checked &&
      !pendingPrivate
    ) {
      setPrivateConfirm(true);
      return;
    }
    if (
      selected &&
      (
        formRef.current?.elements.namedItem(
          "allow_private_network",
        ) as HTMLInputElement
      )?.checked &&
      !selected.allow_private_network &&
      !pendingPrivate
    ) {
      setPrivateConfirm(true);
      return;
    }
    setBusy(true);
    setError("");
    setFields({});
    setNotice("");
    try {
      const f = parseForm(event.currentTarget, kind);
      let result: Destination;
      if (selected) {
        // PATCH carries only changed top-level fields; null clears stored secrets.
        const payload: Record<string, unknown> = {};
        if (f.name !== selected.name) payload.name = f.name;
        if (f.enabled !== selected.enabled) payload.enabled = f.enabled;
        if (f.delivery_mode !== selected.delivery_mode)
          payload.delivery_mode = f.delivery_mode;
        if (f.allow_private_network !== selected.allow_private_network)
          payload.allow_private_network = f.allow_private_network;
        const config: ConfigUpdate = {};
        Object.entries(f.config).forEach(([k, v]) => {
          const old = selected.config[k];
          if (k === "custom_headers" && isEmptyObject(v)) return;
          if (JSON.stringify(v) !== JSON.stringify(old)) config[k] = v;
        });
        // URL is encrypted and can only be replaced, never read back.
        if (Object.keys(config).length) payload.config = config;
        const secrets: Record<string, unknown> = { ...f.secrets };
        if (
          (
            event.currentTarget.elements.namedItem(
              "clear_password",
            ) as HTMLInputElement | null
          )?.checked
        )
          secrets.password = null;
        if (
          (
            event.currentTarget.elements.namedItem(
              "clear_token",
            ) as HTMLInputElement | null
          )?.checked
        )
          secrets.token = null;
        if (
          (
            event.currentTarget.elements.namedItem(
              "clear_header_value",
            ) as HTMLInputElement | null
          )?.checked
        )
          secrets.header_value = null;
        if (Object.keys(secrets).length) payload.secrets = secrets;
        result = await apiRequest<Destination>(`destinations/${selected.id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
      } else
        result = await apiRequest<Destination>("destinations", {
          method: "POST",
          body: JSON.stringify(f),
        });
      setSelected(result);
      setCreating(false);
      setPendingPrivate(false);
      setOnceSecret(result.signing_secret ?? "");
      setNotice(selected ? "Destination saved." : "Destination created.");
      await refresh();
    } catch (e) {
      setError(message(e));
      if (e instanceof ApiError)
        setFields(
          Object.fromEntries(e.fields.map((f) => [f.field, f.message])),
        );
    } finally {
      setBusy(false);
    }
  }
  async function toggle(item: Destination) {
    setBusy(true);
    try {
      await apiRequest(`destinations/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !item.enabled }),
      });
      await refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function test(item: Destination) {
    setBusy(true);
    setNotice("Test send is running…");
    setError("");
    try {
      const result = await apiRequest<{
        outcome: string;
        category?: string;
        http_status?: number | null;
        sanitized_error?: string | null;
        duration_ms?: number;
      }>(`destinations/${item.id}/test`, { method: "POST", body: "{}" });
      setNotice(
        `${result.outcome === "success" ? "Test send: Success" : `Test send: ${result.category ?? result.outcome}`}${result.http_status ? ` · HTTP ${result.http_status}` : ""}${result.sanitized_error ? ` · ${result.sanitized_error}` : ""}${result.duration_ms != null ? ` · ${result.duration_ms} ms` : ""}`,
      );
    } catch (e) {
      setNotice("");
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function rotate(item: Destination) {
    if (
      !window.confirm(
        "Rotate the signing secret? The existing secret will stop working immediately.",
      )
    )
      return;
    setBusy(true);
    try {
      const result = await apiRequest<{ signing_secret: string }>(
        `destinations/${item.id}/rotate-signing-secret`,
        { method: "POST", body: "{}" },
      );
      setOnceSecret(result.signing_secret);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove(item: Destination) {
    if (!window.confirm(`Delete “${item.name}”?`)) return;
    setBusy(true);
    try {
      await apiRequest(`destinations/${item.id}`, { method: "DELETE" });
      setSelected(null);
      setNotice("Destination deleted.");
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.code === "destination_in_use"
          ? "Disable it instead, or remove it from rules and the fallback first."
          : message(e),
      );
    } finally {
      setBusy(false);
    }
  }
  function openPrivate() {
    previousFocus.current = document.activeElement as HTMLElement;
    setPrivateConfirm(true);
  }
  function confirmPrivate() {
    setPendingPrivate(true);
    setPrivateConfirm(false);
    const toggle = formRef.current?.elements.namedItem(
      "allow_private_network",
    ) as HTMLInputElement | null;
    if (toggle) toggle.checked = true;
  }
  return (
    <main className="dashboard-main">
      <section className="dashboard" aria-labelledby="destinations-title">
        <div className="dashboard-heading">
          <div>
            <div className="eyebrow">ADMINISTRATION</div>
            <h1 id="destinations-title" tabIndex={-1}>
              Destinations
            </h1>
            <p className="subtle">Manage where feedback is delivered.</p>
          </div>
          <div className="button-row">
            <button onClick={() => startCreate("webhook")}>New webhook</button>
            <button
              className="quiet-button"
              onClick={() => startCreate("smtp")}
            >
              New SMTP
            </button>
          </div>
        </div>
        {error && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}
        <p role="status" className="notice">
          {notice}
        </p>
        <section className="panel destination-list" aria-label="Destinations">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>State</th>
                  <th>Delivery mode</th>
                  <th>Webhook origin</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <th scope="row">
                      {item.name}
                      {item.is_system && (
                        <small className="system-mark">System inbox</small>
                      )}
                    </th>
                    <td>
                      {item.type === "webhook"
                        ? "Webhook"
                        : item.type === "smtp"
                          ? "SMTP"
                          : "Built-in inbox"}
                    </td>
                    <td>{item.enabled ? "Enabled" : "Disabled"}</td>
                    <td>{modeLabel(item.delivery_mode)}</td>
                    <td>
                      {item.type === "webhook"
                        ? `${item.config.url_origin ?? ""}${item.config.url_has_path ? " · path hidden" : ""}`
                        : "—"}
                    </td>
                    <td>
                      <div className="button-row">
                        {item.is_system ? (
                          <button
                            className="text-button"
                            disabled={busy}
                            onClick={() => void toggle(item)}
                          >
                            {item.enabled ? "Disable" : "Enable"}
                          </button>
                        ) : (
                          <>
                            <button
                              className="text-button"
                              onClick={() => void edit(item)}
                            >
                              Edit
                            </button>
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() => void test(item)}
                            >
                              Test
                            </button>
                            <button
                              className="text-button"
                              onClick={() => void toggle(item)}
                            >
                              {item.enabled ? "Disable" : "Enable"}
                            </button>
                            <button
                              className="text-button"
                              onClick={() => void remove(item)}
                            >
                              Delete
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!items.length && (
            <p className="empty-inline">No destinations yet.</p>
          )}
        </section>
        {selected && !creating && (
          <section className="panel" aria-labelledby="detail-title">
            <div className="panel-heading">
              <h2 id="detail-title">{selected.name}</h2>
              <div className="button-row">
                <button
                  className="quiet-button"
                  onClick={() => void edit(selected)}
                >
                  Edit
                </button>
                {selected.type === "webhook" && selected.config.signing && (
                  <button
                    className="quiet-button"
                    onClick={() => void rotate(selected)}
                  >
                    Rotate signing secret
                  </button>
                )}
              </div>
            </div>
            <p>
              {selected.type === "webhook"
                ? `${selected.config.url_origin ?? ""} · path hidden`
                : selected.type.toUpperCase()}{" "}
              · {modeLabel(selected.delivery_mode)}
            </p>
            {!selected.is_system && (
              <div className="button-row">
                <button onClick={() => void test(selected)}>Test send</button>
                <button
                  className="danger-button"
                  onClick={() => void remove(selected)}
                >
                  Delete
                </button>
              </div>
            )}
          </section>
        )}
        {onceSecret && (
          <section className="secret-reveal" role="alert">
            <strong>Signing secret · shown once</strong>
            <p>
              Copy and store this secret securely. It will not be shown again.
            </p>
            <div className="copy-row">
              <input aria-label="Signing secret" readOnly value={onceSecret} />
              <button
                type="button"
                onClick={() => void navigator.clipboard?.writeText(onceSecret)}
              >
                Copy
              </button>
              <button
                type="button"
                className="quiet-button"
                onClick={() => setOnceSecret("")}
              >
                Dismiss
              </button>
            </div>
          </section>
        )}
        {creating && (
          <section className="panel" aria-labelledby="destination-form-title">
            <h2 id="destination-form-title">
              {selected
                ? "Edit destination"
                : `Create ${kind === "smtp" ? "SMTP" : "webhook"} destination`}
            </h2>
            <form
              ref={formRef}
              className="destination-form"
              onSubmit={submit}
              noValidate
            >
              <Field
                name="name"
                label="Name"
                initial={selected?.name}
                error={fields.name}
              />
              <label className="checkbox-row">
                <input
                  name="enabled"
                  type="checkbox"
                  defaultChecked={selected?.enabled ?? true}
                />{" "}
                Enabled
              </label>
              <label htmlFor="delivery_mode">Delivery mode</label>
              <select
                id="delivery_mode"
                name="delivery_mode"
                defaultValue={selected?.delivery_mode ?? "new_groups_only"}
              >
                <option value="new_groups_only">New groups only</option>
                <option value="every_occurrence">Every occurrence</option>
              </select>
              <p className="help-text">New groups only sends for new or reopened groups; every occurrence sends each report. Reopening a group sends a new notification in either mode.</p>
              {kind === "webhook" ? (
                <>
                  <Field
                    name="url"
                    label="URL"
                    type="url"
                    initial=""
                    placeholder={
                      selected
                        ? `${selected.config.url_origin ?? ""} · path hidden`
                        : ""
                    }
                    error={fields.url}
                    help={
                      selected
                        ? "The saved URL path is hidden. Enter a replacement URL to change it."
                        : undefined
                    }
                  />
                  {selected && (
                    <p className="secret-controls">
                      Saved ·{" "}
                      <label className="replace-label">
                        Replace
                        <input name="url_replace" type="checkbox" />
                      </label>
                    </p>
                  )}
                  <label htmlFor="auth">Authentication</label>
                  <select
                    id="auth"
                    name="auth"
                    defaultValue={selected?.config.auth?.type ?? "none"}
                  >
                    <option value="none">None</option>
                    <option value="bearer">Bearer token</option>
                    <option value="header">Custom header</option>
                  </select>
                  {(selected?.config.auth?.token?.set ||
                    selected?.config.auth?.value?.set) && (
                    <p className="secret-controls">Saved · Replace · Clear</p>
                  )}
                  <Field
                    name="token"
                    label="Bearer token"
                    type="password"
                    error={fields.token}
                  />
                  <Field
                    name="header_name"
                    label="Custom header name"
                    initial={selected?.config.auth?.name}
                    error={fields["auth.name"]}
                  />
                  <Field
                    name="header_value"
                    label="Custom header value"
                    type="password"
                    error={fields["auth.value"]}
                  />
                  {selected?.config.auth?.token?.set && (
                    <label className="checkbox-row">
                      <input name="clear_token" type="checkbox" /> Clear bearer
                      token
                    </label>
                  )}
                  {selected?.config.auth?.value?.set && (
                    <label className="checkbox-row">
                      <input name="clear_header_value" type="checkbox" /> Clear
                      header value
                    </label>
                  )}
                  <label htmlFor="custom_headers">
                    Custom headers (one Name: value per line, up to 10)
                  </label>
                  <textarea
                    id="custom_headers"
                    name="custom_headers"
                    rows={3}
                    defaultValue=""
                    placeholder={(selected?.config.custom_headers ?? [])
                      .map((x: string) => `${x}: [saved]`)
                      .join("\n")}
                  />
                  {(selected?.config.custom_headers ?? []).map(
                    (name: string) => (
                      <label className="checkbox-row" key={name}>
                        <input
                          type="checkbox"
                          name="clear_custom_header"
                          value={name}
                        />{" "}
                        Clear saved {name}
                      </label>
                    ),
                  )}
                  <label className="checkbox-row">
                    <input
                      name="signing"
                      type="checkbox"
                      defaultChecked={selected?.config.signing ?? false}
                    />{" "}
                    Sign webhook requests
                  </label>
                  <Field
                    name="timeout_seconds"
                    label="Timeout (seconds)"
                    type="number"
                    initial={selected?.config.timeout_seconds ?? 10}
                    error={fields.timeout_seconds}
                  />
                </>
              ) : (
                <>
                  <Field
                    name="host"
                    label="Host"
                    initial={selected?.config.host}
                    error={fields.host}
                  />
                  <Field
                    name="port"
                    label="Port"
                    type="number"
                    initial={selected?.config.port ?? 587}
                    error={fields.port}
                  />
                  <label htmlFor="tls_mode">TLS mode</label>
                  <select
                    id="tls_mode"
                    name="tls_mode"
                    defaultValue={selected?.config.tls_mode ?? "starttls"}
                    onChange={(e) => {
                      setTlsNone(e.target.value === "none");
                      const form = e.currentTarget.form;
                      if (e.target.value === "none") {
                        (
                          form?.elements.namedItem(
                            "username",
                          ) as HTMLInputElement
                        ).value = "";
                        (
                          form?.elements.namedItem(
                            "password",
                          ) as HTMLInputElement
                        ).value = "";
                      }
                    }}
                  >
                    <option value="starttls">STARTTLS</option>
                    <option value="implicit_tls">Implicit TLS</option>
                    <option value="none">None (private relay only)</option>
                  </select>
                  <p className="inline-note">
                    TLS None is for a relay on a private network only and needs
                    Allow private network. Username and password are hidden and
                    cleared when selected.
                  </p>
                  {!tlsNone && (
                    <>
                      <Field
                        name="username"
                        label="Username"
                        initial={
                          typeof selected?.config.username === "string"
                            ? selected.config.username
                            : ""
                        }
                        error={fields.username}
                      />
                      <SecretField
                        name="password"
                        label="Password"
                        saved={Boolean(selected?.config.password?.set)}
                        error={fields.password}
                      />
                    </>
                  )}
                  {selected?.config.password?.set && (
                    <label className="checkbox-row">
                      <input name="clear_password" type="checkbox" /> Clear
                      password
                    </label>
                  )}
                  {tlsNone && selected?.config.username && (
                    <label className="checkbox-row">
                      <input name="clear_username" type="checkbox" /> Clear
                      saved username
                    </label>
                  )}
                  <Field
                    name="from"
                    label="Sender address"
                    initial={selected?.config.from}
                    error={fields.from}
                  />
                  <label htmlFor="to">Recipients (1–20, one per line)</label>
                  <textarea
                    id="to"
                    name="to"
                    rows={3}
                    defaultValue={(selected?.config.to ?? []).join("\n")}
                    aria-describedby={fields.to ? "err-to" : undefined}
                  />
                  {fields.to && (
                    <small id="err-to" className="field-error">
                      {fields.to}
                    </small>
                  )}
                  <Field
                    name="timeout_seconds"
                    label="Timeout (seconds)"
                    type="number"
                    initial={selected?.config.timeout_seconds ?? 15}
                    error={fields.timeout_seconds}
                  />
                </>
              )}
              <label className="checkbox-row">
                <input
                  name="allow_private_network"
                  type="checkbox"
                  defaultChecked={selected?.allow_private_network ?? false}
                  onChange={(e) => {
                    if (e.target.checked && !pendingPrivate) {
                      e.target.checked = false;
                      openPrivate();
                    }
                  }}
                />{" "}
                Allow private network
              </label>
              <p className="help-text">
                {kind === "smtp"
                  ? "Needed for a relay on a private or loopback address, a non-standard port, or TLS None."
                  : "Only enable for a trusted private endpoint."}
              </p>
              <div className="button-row">
                <button disabled={busy}>
                  {busy
                    ? "Saving…"
                    : selected
                      ? "Save changes"
                      : "Create destination"}
                </button>
                <button
                  type="button"
                  className="quiet-button"
                  onClick={() => {
                    setCreating(false);
                    setPendingPrivate(false);
                    setFields({});
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          </section>
        )}
        {privateConfirm && (
          <div className="dialog-backdrop">
            <section
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="private-title"
              className="confirm-dialog"
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setPrivateConfirm(false);
                  previousFocus.current?.focus();
                }
              }}
            >
              <h2 id="private-title">Allow private network access?</h2>
              <p>
                This destination may reach internal services. Metadata and
                link-local addresses remain blocked.
              </p>
              <div className="button-row">
                <button onClick={confirmPrivate}>Allow private network</button>
                <button
                  className="quiet-button"
                  onClick={() => {
                    setPrivateConfirm(false);
                    previousFocus.current?.focus();
                  }}
                >
                  Cancel
                </button>
              </div>
            </section>
          </div>
        )}
      </section>
    </main>
  );
}
function Field({
  name,
  label,
  type = "text",
  initial = "",
  error,
  help,
  placeholder,
}: {
  name: string;
  label: string;
  type?: string;
  initial?: string | number;
  error?: string;
  help?: string;
  placeholder?: string;
}) {
  const id = `dest-${name}`;
  const errorId = `err-${name}`;
  return (
    <>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        name={name}
        type={type}
        defaultValue={initial}
        placeholder={placeholder}
        aria-invalid={Boolean(error)}
        aria-describedby={
          [help ? `${id}-help` : "", error ? errorId : ""]
            .filter(Boolean)
            .join(" ") || undefined
        }
      />
      {help && (
        <small id={`${id}-help`} className="subtle">
          {help}
        </small>
      )}
      {error && (
        <small id={errorId} className="field-error">
          {error}
        </small>
      )}
    </>
  );
}
function SecretField({
  name,
  label,
  saved,
  error,
}: {
  name: string;
  label: string;
  saved: boolean;
  error?: string;
}) {
  return (
    <>
      <label htmlFor={`dest-${name}`}>{label}</label>
      {saved && <p className="secret-controls">Saved · Replace · Clear</p>}
      <input
        id={`dest-${name}`}
        type="password"
        name={name}
        autoComplete="new-password"
        aria-describedby={error ? `err-${name}` : undefined}
      />
      {error && (
        <small className="field-error" id={`err-${name}`}>
          {error}
        </small>
      )}
    </>
  );
}
function message(error: unknown) {
  return error instanceof ApiError && error.status === 401
    ? "Your session has expired. Sign in again to continue."
    : error instanceof Error
      ? error.message
      : "Something went wrong.";
}
function isEmptyObject(value: unknown): value is Record<string, never> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.keys(value).length === 0
  );
}
