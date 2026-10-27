export class KebivoError extends Error {}

export class KebivoHTTPError extends KebivoError {
  constructor(status_code, code, message, details = null, request_id = null) {
    super(message);
    this.status_code = status_code;
    this.code = code;
    this.details = details;
    this.request_id = request_id;
  }
}

export class KebivoConnectionError extends KebivoError {}

function retryAfter(value) {
  if (!value) return 0;
  const numeric = Number(value);
  const seconds = Number.isFinite(numeric) ? numeric : (Date.parse(value) - Date.now()) / 1000;
  return Number.isFinite(seconds) ? Math.min(60, Math.max(0, seconds)) : 0;
}

const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

export class Kebivo {
  constructor(url, token, { timeout = 10 } = {}) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      throw new TypeError("url must be an HTTP(S) base URL");
    }
    if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.search || parsed.hash || parsed.username || parsed.password) {
      throw new TypeError("url must be an HTTP(S) base URL");
    }
    if (!token) throw new TypeError("token is required");
    if (!Number.isFinite(timeout) || timeout <= 0) throw new TypeError("timeout must be positive");
    this.url = `${url.replace(/\/+$/, "")}/api/v1/feedback`;
    this.token = token;
    this.timeout = timeout;
  }

  report_gap(question, options = {}) {
    return this._submit("knowledge_gap", question, options);
  }

  submit_correction(question, correction, options = {}) {
    return this._submit("knowledge_correction", question, { ...options, correction });
  }

  async _submit(event_type, question, options) {
    const { idempotency_key, ...fields } = options;
    // Like the Python SDK, null means "not set" rather than a literal value.
    const present = Object.entries(fields).filter(([, value]) => value != null);
    const body = JSON.stringify({ ...Object.fromEntries(present), event_type, question });
    const headers = {
      Authorization: `Bearer ${this.token}`,
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency_key ?? crypto.randomUUID(),
    };
    for (let attempt = 0; attempt < 4; attempt++) {
      let response;
      try {
        response = await fetch(this.url, {
          method: "POST", headers, body, redirect: "manual",
          signal: AbortSignal.timeout(this.timeout * 1000),
        });
      } catch (error) {
        if (attempt === 3) throw new KebivoConnectionError(String(error), { cause: error });
        await sleep(2 ** attempt);
        continue;
      }
      if (response.ok) {
        try {
          return await response.json();
        } catch (error) {
          throw new KebivoError("Kebivo returned a response that is not JSON", { cause: error });
        }
      }
      let envelope = {};
      try {
        envelope = (await response.json())?.error ?? {};
      } catch {
        // Proxies can return plain text or an empty body.
      }
      const failure = new KebivoHTTPError(
        response.status, envelope.code ?? null,
        envelope.message || response.statusText || "Request failed", envelope.details ?? null,
        envelope.request_id ?? response.headers.get("X-Request-ID"),
      );
      if ((response.status !== 429 && response.status < 500) || attempt === 3) throw failure;
      await sleep(Math.max(2 ** attempt, retryAfter(response.headers.get("Retry-After"))));
    }
  }
}
