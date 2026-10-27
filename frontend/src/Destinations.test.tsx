import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Destinations } from "./Destinations";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const inbox = {
  id: "inbox",
  name: "Built-in inbox",
  type: "inbox",
  enabled: true,
  delivery_mode: "new_groups_only",
  allow_private_network: false,
  is_system: true,
  config: {},
};
const hook = {
  id: "hook",
  name: "Events",
  type: "webhook",
  enabled: true,
  delivery_mode: "new_groups_only",
  allow_private_network: false,
  is_system: false,
  config: {
    url_origin: "https://example.test",
    url_has_path: true,
    auth: { type: "none" },
    signing: false,
    custom_headers: ["X-Token"],
    custom_header_secrets: { "X-Token": { set: true } },
    timeout_seconds: 10,
  },
};
const smtp = {
  id: "smtp",
  name: "Mail relay",
  type: "smtp",
  enabled: true,
  delivery_mode: "new_groups_only",
  allow_private_network: false,
  is_system: false,
  config: {
    host: "mail.example.test",
    port: 587,
    tls_mode: "starttls",
    username: { set: true },
    password: { set: true },
    from: "sender@example.test",
    to: ["recipient@example.test"],
    timeout_seconds: 15,
  },
};
const slack = {
  id: "slack",
  name: "Team updates",
  type: "slack",
  enabled: true,
  delivery_mode: "new_groups_only",
  allow_private_network: false,
  is_system: false,
  config: {
    url: { set: true },
    url_origin: "https://hooks.slack.com",
    url_has_path: true,
    timeout_seconds: 10,
  },
};
describe("Destinations admin UI", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: { path: string; method: string; body: string }[] = [];
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    requests = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input),
        method = String(init?.method ?? "GET"),
        body = String(init?.body ?? "");
      requests.push({ path, method, body });
      if (path.endsWith("/destinations") && method === "POST") {
        const payload = JSON.parse(body);
        return new Response(
          JSON.stringify({
            ...(payload.type === "slack" ? slack : hook),
            id: "new",
            name: payload.name,
            signing_secret: payload.type === "webhook" ? "once-only" : undefined,
          }),
        );
      }
      if (path.endsWith("/destinations"))
        return new Response(JSON.stringify({ items: [inbox, hook, smtp, slack] }));
      if (path.endsWith("/destinations/hook") && method === "GET")
        return new Response(JSON.stringify(hook));
      if (path.endsWith("/destinations/hook") && method === "PATCH")
        return new Response(JSON.stringify(hook));
      if (path.endsWith("/destinations/smtp") && method === "GET")
        return new Response(JSON.stringify(smtp));
      if (path.endsWith("/destinations/smtp") && method === "PATCH")
        return new Response(JSON.stringify(smtp));
      if (path.endsWith("/destinations/slack") && method === "GET")
        return new Response(JSON.stringify(slack));
      if (path.endsWith("/destinations/slack") && method === "PATCH")
        return new Response(JSON.stringify(slack));
      if (path.endsWith("/destinations/new") && method === "GET")
        return new Response(JSON.stringify({ ...slack, id: "new" }));
      if (path.endsWith("/test"))
        return new Response(
          JSON.stringify({
            outcome: "permanent",
            category: "timeout",
            sanitized_error: "Timed out safely",
            http_status: null,
            duration_ms: 15,
          }),
        );
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });
  async function render() {
    await act(async () => {
      root.render(<Destinations />);
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  it("renders destination types, masked URL origin and system inbox actions", async () => {
    await render();
    expect(container.textContent).toContain("Built-in inbox");
    expect(container.textContent).toContain("Webhook");
    expect(container.textContent).toContain("Slack");
    expect(container.textContent).toContain("https://hooks.slack.com · path hidden");
    expect(container.textContent).toContain(
      "https://example.test · path hidden",
    );
    expect(container.textContent).toContain("System inbox");
    expect(container.querySelector("tbody tr")?.textContent).toContain(
      "Disable",
    );
  });
  it("creates Slack with an encrypted URL and supports test send", async () => {
    await render();
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent === "New Slack")!
        .click();
    });
    expect(container.textContent).toContain("Create Slack destination");
    expect(
      container.querySelector('input[name="allow_private_network"]'),
    ).toBeNull();
    await act(async () => {
      container.querySelector<HTMLInputElement>('input[name="name"]')!.value =
        "Team updates";
      container
        .querySelector<HTMLInputElement>('input[name="name"]')!
        .dispatchEvent(new Event("input", { bubbles: true }));
      container.querySelector<HTMLInputElement>('input[name="url"]')!.value =
        "https://hooks.slack.com/services/T123/B456/secret";
      container
        .querySelector<HTMLInputElement>('input[name="url"]')!
        .dispatchEvent(new Event("input", { bubbles: true }));
      container
        .querySelector("form")!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await new Promise((r) => setTimeout(r, 0));
    });
    const post = requests.find(
      (request) => request.method === "POST" && request.path.endsWith("/destinations"),
    );
    expect(JSON.parse(post!.body)).toMatchObject({
      type: "slack",
      config: { timeout_seconds: 10 },
      secrets: { url: "https://hooks.slack.com/services/T123/B456/secret" },
    });
    expect(container.textContent).toContain("https://hooks.slack.com · path hidden");
    expect(container.textContent).not.toContain(
      "https://hooks.slack.com/services/T123/B456/secret",
    );
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent === "Test send")!
        .click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(
      requests.some(
        (request) =>
          request.method === "POST" && request.path.endsWith("/destinations/new/test"),
      ),
    ).toBe(true);
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".panel-heading button")!.click();
      await new Promise((r) => setTimeout(r, 0));
    });
    const url = container.querySelector<HTMLInputElement>('input[name="url"]')!;
    expect(url.value).toBe("");
    expect(url.placeholder).toBe("https://hooks.slack.com · path hidden");
  });
  it("creates a webhook and clears the one-time secret when dismissed", async () => {
    await render();
    await act(async () => {
      container.querySelector("button")!.click();
    });
    const inputs = container.querySelectorAll<HTMLInputElement>("input");
    const name = [...inputs].find((x) => x.name === "name")!;
    const url = [...inputs].find((x) => x.name === "url")!;
    await act(async () => {
      name.value = "New endpoint";
      name.dispatchEvent(new Event("input", { bubbles: true }));
      url.value = "https://hooks.example.test/path";
      url.dispatchEvent(new Event("input", { bubbles: true }));
      container
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      await new Promise((r) => setTimeout(r, 0));
    });
    const post = requests.find(
      (x) => x.method === "POST" && x.path.endsWith("/destinations"),
    );
    expect(JSON.parse(post!.body).secrets.url).toBe(
      "https://hooks.example.test/path",
    );
    expect(container.textContent).toContain("Signing secret · shown once");
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((b) => b.textContent === "Dismiss")!
        .click();
    });
    expect(container.textContent).not.toContain("once-only");
  });
  it("requires private-network confirmation before enabling the option", async () => {
    await render();
    await act(async () => {
      container.querySelector<HTMLButtonElement>("button")!.click();
    });
    await act(async () => {
      container
        .querySelector<HTMLInputElement>('input[name="allow_private_network"]')!
        .click();
    });
    expect(container.textContent).toContain(
      "Metadata and link-local addresses remain blocked",
    );
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((b) => b.textContent === "Allow private network")!
        .click();
    });
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(
      container.querySelector<HTMLInputElement>(
        'input[name="allow_private_network"]',
      )!.checked,
    ).toBe(true);
  });
  it("hides and clears SMTP credentials when TLS is None", async () => {
    await render();
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent === "New SMTP")!
        .click();
    });
    const tls = container.querySelector<HTMLSelectElement>(
      'select[name="tls_mode"]',
    )!;
    await act(async () => {
      tls.value = "none";
      tls.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(container.querySelector('input[name="username"]')).toBeNull();
    expect(container.querySelector('input[name="password"]')).toBeNull();
    expect(container.textContent).toContain("private relay only");
  });
  it("clears saved SMTP credentials when changing to TLS None", async () => {
    await render();
    const row = [...container.querySelectorAll("tbody tr")].find((item) =>
      item.textContent?.includes("Mail relay"),
    )!;
    await act(async () => {
      [...row.querySelectorAll("button")]
        .find((button) => button.textContent === "Edit")!
        .click();
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      const tls = container.querySelector<HTMLSelectElement>(
        'select[name="tls_mode"]',
      )!;
      tls.value = "none";
      tls.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(
      container.querySelector('input[name="clear_username"]'),
    ).toBeTruthy();
    await act(async () => {
      container.querySelector<HTMLInputElement>(
        'input[name="clear_username"]',
      )!.checked = true;
      container.querySelector<HTMLInputElement>(
        'input[name="clear_password"]',
      )!.checked = true;
      container
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      await new Promise((r) => setTimeout(r, 0));
    });
    const patch = requests.find(
      (request) => request.method === "PATCH" && request.path.endsWith("/smtp"),
    );
    const body = JSON.parse(patch!.body);
    expect(body.config.username).toBeNull();
    expect(body.secrets.password).toBeNull();
  });
  it("shows test result category and sanitized message", async () => {
    await render();
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((b) => b.textContent === "Test")!
        .click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(container.textContent).toContain(
      "Test send: timeout · Timed out safely",
    );
  });
  it("keeps untouched saved custom headers out of edits and supports clearing", async () => {
    await render();
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent === "Edit")!
        .click();
      await new Promise((r) => setTimeout(r, 0));
    });
    const textarea = container.querySelector<HTMLTextAreaElement>(
      'textarea[name="custom_headers"]',
    )!;
    expect(
      container.querySelector(
        'input[name="clear_custom_header"][value="X-Token"]',
      ),
    ).toBeTruthy();
    await act(async () => {
      textarea.value = "X-New: replacement";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      container
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      await new Promise((r) => setTimeout(r, 0));
    });
    const patch = requests.find(
      (request) => request.method === "PATCH" && request.path.endsWith("/hook"),
    );
    expect(JSON.parse(patch!.body).config.custom_headers).toEqual({
      "X-New": "replacement",
    });
  });
});
