import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Routing } from "./Routing";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const project = {
  id: "p",
  tags: [{ name: "Urgent", slug: "urgent" }],
  fallback_destination_id: null,
};
const rule = {
  id: "r1",
  name: "Urgent gap",
  enabled: true,
  event_types: ["knowledge_gap"],
  match_mode: "any",
  tags: ["urgent"],
  priority: 1,
  destination_ids: ["d1"],
  match_behavior: "stop",
  created_at: "2026-01-01",
};
describe("Routing UI", () => {
  let root: Root;
  let container: HTMLDivElement;
  let requests: { path: string; method: string; body: string }[] = [];
  let simulated = false;
  let failFallback = false;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    requests = [];
    simulated = false;
    failFallback = false;
    globalThis.fetch = vi.fn(async (input, init) => {
      const path = String(input),
        method = String(init?.method ?? "GET"),
        body = String(init?.body ?? "");
      requests.push({ path, method, body });
      if (path.endsWith("/routing-rules"))
        return new Response(JSON.stringify({ items: [rule] }));
      if (path.endsWith("/destinations"))
        return new Response(
          JSON.stringify({
            items: [{ id: "d1", name: "Inbox", enabled: true }],
          }),
        );
      if (path.endsWith("/routing/simulate")) {
        simulated = true;
        return new Response(
          JSON.stringify({
            evaluated_rules: [
              {
                rule_id: "r1",
                status: "matched",
                reason: "Event type and tags match",
              },
            ],
            stopped_by_rule_id: "r1",
            destinations: [
              {
                destination_id: "d1",
                via: "rule",
                outcome: "delivery_created",
                rule_id: "r1",
              },
              {
                destination_id: "d2",
                via: "rule",
                outcome: "duplicate_destination",
                rule_id: "r1",
              },
              {
                destination_id: "d3",
                via: "fallback",
                outcome: "fallback_skipped_disabled",
                rule_id: null,
              },
            ],
            fallback_applied: false,
            warnings: ["Destination disabled"],
          }),
        );
      }
      if (path.endsWith("/projects/p") && method === "PATCH" && failFallback)
        return new Response(JSON.stringify({ message: "Save failed" }), {
          status: 500,
        });
      return new Response(JSON.stringify(project));
    }) as typeof fetch;
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });
  async function render() {
    await act(async () => {
      root.render(<Routing project={project} />);
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  it("shows ordered rules, stop behavior and normalized tag preview", async () => {
    await render();
    expect(container.textContent).toContain("Urgent gap");
    expect(container.textContent).toContain("stop");
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((x) => x.textContent === "Add rule")!
        .click();
    });
    const input = container.querySelector<HTMLInputElement>(
      '.routing-form input[aria-describedby="tag-preview"]',
    )!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!.call(input, " My TAG ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.textContent).toContain("my-tag");
  });
  it("posts a dry run and renders match, stop, fallback and warnings", async () => {
    await render();
    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((x) => x.textContent === "Simulate")!
        .click();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(simulated).toBe(true);
    expect(container.textContent).toContain("matched");
    expect(container.textContent).toContain("Destinations: Inbox (rule)");
    expect(container.textContent).toContain("duplicate");
    expect(container.textContent).toContain("Skipped: d3 — disabled");
    expect(container.textContent).toContain("Stop rule");
    expect(container.textContent).toContain("Fallback applied: No");
    expect(container.textContent).toContain("Warning: Destination disabled");
  });
  it("sets and clears fallback through the project API", async () => {
    await render();
    const select = container.querySelector<HTMLSelectElement>(
      'select[aria-label="Fallback destination"]',
    )!;
    await act(async () => {
      select.value = "d1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 0));
      select.value = "";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 0));
    });
    const updates = requests.filter((x) => x.method === "PATCH");
    expect(JSON.parse(updates[0].body)).toEqual({
      fallback_destination_id: "d1",
    });
    expect(JSON.parse(updates[1].body)).toEqual({
      fallback_destination_id: null,
    });
  });
  it("reverts the fallback select when saving fails", async () => {
    await render();
    failFallback = true;
    const select = container.querySelector<HTMLSelectElement>(
      'select[aria-label="Fallback destination"]',
    )!;
    await act(async () => {
      select.value = "d1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(select.value).toBe("");
  });
});
