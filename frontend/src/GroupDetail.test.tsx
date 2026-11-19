import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { GroupDetail } from "./GroupDetail";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
describe("group detail safety and sections", () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    globalThis.fetch = vi.fn(async (input) => {
      const url = String(input);
      if (url.includes("/admin/groups/g1/events"))
        return new Response(
          JSON.stringify({
            items: [
              {
                id: "e1",
                event_type: "knowledge_gap",
                match: { method: "exact", triggered_reopen: true },
                agent_reported: {
                  question: "<script>alert(1)</script>",
                  correction: "<img onerror=alert(1)>",
                  metadata: { note: "<script>meta</script>" },
                },
                system: { submitted_via: "rest", received_at: "2026-01-02T03:04:05Z" },
                tags: [
                  {
                    slug: "it",
                    name: "IT",
                    from_default: false,
                    from_submission: false,
                    from_admin: true,
                  },
                ],
              },
            ],
            next_cursor: null,
          }),
        );
      if (url.includes("/admin/groups/g1/history"))
        return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id="))
        return new Response(JSON.stringify({ items: [
          { id: "d1", event_id: "e1", status: "failed", attempt_count: 1, destination_id: "dst" },
          { id: "d2", event_id: "e1", status: "delivered", attempt_count: 1, destination_id: "dst" },
        ] }));
      return new Response(
        JSON.stringify({
          id: "g1",
          status: "open",
          occurrence_count: 1,
          representative: { event_id: "e1", question: "q" },
          project: { name: "Support" },
          event_type: "knowledge_gap",
          notes: [
            {
              id: "n1",
              body: "<img onerror=alert(2)>",
              created_at: "2026-01-01T00:00:00Z",
            },
          ],
          tags: [],
        }),
      );
    }) as typeof fetch;
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });
  it("renders distinct sections and treats feedback, metadata and notes as literal text", async () => {
    await act(async () => {
      root.render(<GroupDetail groupId="g1" navigate={() => {}} />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    for (const title of [
      "Administrator notes",
      "Technical details",
    ])
      expect(container.textContent).toContain(title);
    expect(container.querySelector("h1")?.textContent).toBe("q");
    expect(container.textContent).toContain("1 occurrence");
    expect(container.textContent).toContain("Exact match");
    expect(container.textContent).toContain("Reopened this group");
    const reportedAt = container.querySelector("#event-e1 time");
    expect(reportedAt?.getAttribute("datetime")).toBe("2026-01-02T03:04:05Z");
    expect(reportedAt?.textContent).toBe(new Date("2026-01-02T03:04:05Z").toLocaleString());
    expect(container.querySelector("details")?.open).toBe(false);
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.textContent).not.toContain("<img onerror=alert(1)>");
    expect(container.textContent).toContain("<script>meta</script>");
    expect(container.textContent).toContain("<img onerror=alert(2)>");
    expect(container.querySelector("script, img")).toBeNull();
    expect(container.innerHTML).not.toContain("dangerouslySetInnerHTML");
    expect(Array.from(container.querySelectorAll("button")).filter((button) => button.textContent === "Retry")).toHaveLength(1);
  });

  it("labels each occurrence match method", async () => {
    const methods = ["initial", "exact", "semantic", "manual_merge", "manual_split"] as const;
    globalThis.fetch = vi.fn(async (input) => {
      const url = String(input);
      if (url.includes("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: methods.map((method, index) => ({ id: `e${index}`, event_type: "knowledge_gap", match: { method, triggered_reopen: false }, agent_reported: { question: `Question ${index}` }, system: {}, tags: [] })), next_cursor: null }));
      if (url.includes("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: methods.length, representative: { event_id: "e0", question: "Question" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    for (const label of ["Initial", "Exact match", "Semantic match", "Merged", "Split"]) expect(container.textContent).toContain(label);
  });

  it("returns to the inbox with the current filters", async () => {
    history.replaceState({}, "", "/inbox/groups/g1?q=vpn&status=open");
    const navigate = vi.fn();
    await act(async () => {
      root.render(<GroupDetail groupId="g1" navigate={navigate} />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".feedback-detail > button")?.click();
    });
    expect(navigate).toHaveBeenCalledWith("/inbox?q=vpn&status=open");
  });

  it("announces a saved status and asks before deleting an administrator note", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/admin/groups/g1/notes/n1") && init?.method === "DELETE") return new Response(JSON.stringify({}));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 1, representative: { event_id: "e1", question: "How do I reset it?" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [{ id: "n1", body: "Private note", created_at: "2026-01-01T00:00:00Z" }], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const resolved = Array.from(container.querySelectorAll<HTMLButtonElement>(".group-status button")).find(button => button.textContent === "Resolved")!;
    await act(async () => { resolved.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.querySelector('[role="status"]')?.textContent).toMatch(/Changes saved/);
    expect(requests.some(request => request.url.endsWith("/admin/groups/g1") && request.init?.method === "PATCH" && request.init.body?.toString().includes('"resolved"'))).toBe(true);
    const noteField = container.querySelector("#group-note") as HTMLTextAreaElement;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(noteField, "Follow up privately"); noteField.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => { (Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Add note") as HTMLButtonElement).click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(requests.some(request => request.url.endsWith("/admin/groups/g1/notes") && request.init?.method === "POST")).toBe(true);
    expect(container.querySelector('[role="status"]')?.textContent).toMatch(/Changes saved/);
    const tagField = container.querySelector("#group-tag") as HTMLInputElement;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(tagField, "billing"); tagField.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => { (Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Add tag") as HTMLButtonElement).click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(requests.some(request => request.url.endsWith("/admin/groups/g1/tags") && request.init?.method === "PATCH")).toBe(true);
    expect(container.querySelector('[role="status"]')?.textContent).toMatch(/Changes saved/);
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Delete note")!.click(); });
    expect(container.textContent).toContain("Delete this note?");
    expect(requests.some(request => request.url.endsWith("/notes/n1") && request.init?.method === "DELETE")).toBe(false);
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Delete")!.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(requests.some(request => request.url.endsWith("/notes/n1") && request.init?.method === "DELETE")).toBe(true);
  });

  it("confirms deletion with consequences, reports busy feedback, and restores focus", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    const navigate = vi.fn();
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [{ id: "e1", event_type: "knowledge_gap", agent_reported: {}, system: {}, tags: [] }], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/admin/feedback/e1")) return new Response(JSON.stringify({ error: { code: "feedback_busy", message: "Feedback is being processed" } }), { status: 409 });
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 1, representative: { event_id: "e1", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={navigate} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const trigger = Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Delete occurrence")!;
    await act(async () => { trigger.click(); });
    expect(container.textContent).toContain("last occurrence");
    expect(container.textContent).toContain("Copies already sent");
    await act(async () => { (container.querySelector('[role="dialog"] button.danger-button') as HTMLButtonElement).click(); });
    expect(container.textContent).toContain("being delivered, try again shortly");
    await act(async () => { (container.querySelector('[role="dialog"] button.quiet-button') as HTMLButtonElement).click(); await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(document.activeElement).toBe(trigger);
    expect(requests.some(request => request.url.endsWith("/admin/feedback/e1"))).toBe(true);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("blocks splitting every occurrence and submits a selected subset then navigates", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    const navigate = vi.fn();
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [{ id: "e1", event_type: "knowledge_gap", agent_reported: { question: "e1" }, system: {}, tags: [] }, { id: "e2", event_type: "knowledge_gap", agent_reported: { question: "e2" }, system: {}, tags: [] }], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/admin/groups/g1/split")) return new Response(JSON.stringify({ group_id: "g2" }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 2, representative: { event_id: "e1", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={navigate} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const boxes = Array.from(container.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[];
    await act(async () => { boxes[0].click(); boxes[1].click(); });
    const splitButton = Array.from(container.querySelectorAll("button")).find(button => button.textContent?.startsWith("Split selected"))!;
    expect(splitButton.disabled).toBe(true);
    await act(async () => { boxes[1].click(); });
    await act(async () => { splitButton.click(); });
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Confirm split")!.click(); });
    const splitRequest = requests.find(request => request.url.endsWith("/admin/groups/g1/split"))!;
    expect(JSON.parse(String(splitRequest.init?.body))).toEqual({ event_ids: ["e1"] });
    expect(navigate).toHaveBeenCalledWith("/inbox/groups/g2");
  });

  it("merges compatible groups with the expected body and navigates to the target", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    const navigate = vi.fn();
    let merged = false;
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/merge")) { merged = true; return new Response(JSON.stringify({})); }
      if (url.includes("/admin/groups?")) return new Response(JSON.stringify({ items: [{ id: "g2", occurrence_count: 1, representative: { question_excerpt: "Another question" } }] }));
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: merged ? [{ id: "e1", event_type: "knowledge_gap", agent_reported: { question: "e1" }, system: {}, tags: [] }, { id: "e2", event_type: "knowledge_gap", agent_reported: { question: "e2" }, system: {}, tags: [] }, { id: "e3", event_type: "knowledge_gap", agent_reported: { question: "e3" }, system: {}, tags: [] }] : [], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: merged ? 3 : 2, representative: { event_id: "e1", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={navigate} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Merge groups")!.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    await act(async () => { checkbox.click(); });
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Confirm merge")!.click(); });
    const mergeRequest = requests.find(request => request.url.endsWith("/admin/groups/g1/merge"))!;
    expect(JSON.parse(String(mergeRequest.init?.body))).toEqual({ source_group_ids: ["g2"] });
    expect(navigate).toHaveBeenCalledWith("/inbox/groups/g1");
    expect(container.textContent).toContain("3 occurrences");
    expect(container.textContent).toContain("e3");
    expect(requests.filter(request => request.url.endsWith("/admin/groups/g1/events"))).toHaveLength(2);
  });

  it("shows identical reports once, keeps a reopening report apart, and splits a collapsed cluster in one step", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    const same = (id: string, at: string) => ({ id, event_type: "knowledge_gap", agent_reported: { question: "Do you ship to Canada?" }, system: { received_at: at }, tags: [] });
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [same("e1", "2026-11-01T00:00:00Z"), same("e2", "2026-11-04T00:00:00Z"), { id: "e3", event_type: "knowledge_gap", agent_reported: { question: "Ship to Canada?" }, system: {}, tags: [] }, same("e4", "2026-11-03T00:00:00Z"), { ...same("e5", "2026-11-05T00:00:00Z"), match: { method: "exact", triggered_reopen: true } }], next_cursor: "next" }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/admin/groups/g1/split")) return new Response(JSON.stringify({ group_id: "g2" }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 5, representative: { event_id: "e2", question: "Do you ship to Canada?" }, project: { id: "p1", name: "Shop" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    const cards = () => Array.from(container.querySelectorAll("article[id^='event-']")).map(card => card.id);
    expect(cards()).toEqual(["event-e2", "event-e3", "event-e5"]);
    expect(container.querySelector("#event-e5")!.textContent).toContain("Reopened this group");
    const summary = container.querySelector(".identical-reports")!;
    expect(summary.textContent).toMatch(/^2 more loaded reports with identical text, last /);
    expect(summary.querySelector("time")?.getAttribute("dateTime")).toBe("2026-11-04T00:00:00Z");
    const toggle = summary.querySelector("button[aria-expanded]")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.textContent).toBe("Show all 3 loaded");
    expect(summary.textContent).toContain("More occurrences may follow.");
    await act(async () => { Array.from(summary.querySelectorAll("button")).find(button => button.textContent === "Select all 3 for split")!.click(); });
    expect(summary.textContent).toContain("3 selected for split");
    expect(container.querySelector('[aria-label="Select occurrence e1 for split"]')).toBeNull();
    expect(container.querySelector<HTMLDetailsElement>(".triage-more")!.open).toBe(true);
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Split selected occurrences (3)")!.click(); });
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Confirm split")!.click(); });
    expect(JSON.parse(String(requests.find(request => request.url.endsWith("/admin/groups/g1/split"))!.init?.body))).toEqual({ event_ids: ["e1", "e2", "e4"] });
  });

  it("keeps empty reports separate because missing text does not prove they are identical", async () => {
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: ["e1", "e2"].map(id => ({ id, event_type: "knowledge_gap", agent_reported: {}, system: {}, tags: [] })), next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 2, representative: { event_id: "e1", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(Array.from(container.querySelectorAll("article[id^='event-']")).map(card => card.id)).toEqual(["event-e1", "event-e2"]);
    expect(container.querySelector(".identical-reports")).toBeNull();
  });

  it("clears split selection when navigation changes the group", async () => {
    globalThis.fetch = vi.fn(async input => {
      const url = String(input);
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [{ id: "e1", event_type: "knowledge_gap", agent_reported: { question: "e1" }, system: {}, tags: [] }, { id: "e2", event_type: "knowledge_gap", agent_reported: { question: "e2" }, system: {}, tags: [] }], next_cursor: null }));
      if (url.endsWith("/admin/groups/g2/events")) return new Response(JSON.stringify({ items: [{ id: "e2", event_type: "knowledge_gap", agent_reported: {}, system: {}, tags: [] }], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history") || url.endsWith("/admin/groups/g2/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      const id = url.includes("/admin/groups/g2") ? "g2" : "g1";
      return new Response(JSON.stringify({ id, status: "open", occurrence_count: id === "g2" ? 1 : 2, representative: { event_id: "e2", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => { (container.querySelector('[aria-label="Select occurrence e1 for split"]') as HTMLInputElement).click(); });
    expect(container.textContent).toContain("Split selected occurrences (1)");
    await act(async () => { root.render(<GroupDetail groupId="g2" navigate={() => {}} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.textContent).toContain("Split selected occurrences (0)");
    expect((container.querySelector('[aria-label="Select occurrence e2 for split"]') as HTMLInputElement).checked).toBe(false);
  });

  it("deletes the last occurrence without a request body and returns to the inbox", async () => {
    const requests: { url: string; init?: RequestInit }[] = [];
    const navigate = vi.fn();
    globalThis.fetch = vi.fn(async (input, init) => {
      const url = String(input); requests.push({ url, init });
      if (url.endsWith("/admin/groups/g1/events")) return new Response(JSON.stringify({ items: [{ id: "e1", event_type: "knowledge_gap", agent_reported: {}, system: {}, tags: [] }], next_cursor: null }));
      if (url.endsWith("/admin/groups/g1/history")) return new Response(JSON.stringify({ items: [] }));
      if (url.includes("/deliveries?group_id=")) return new Response(JSON.stringify({ items: [] }));
      if (url.endsWith("/admin/feedback/e1")) return new Response(JSON.stringify({ group_id: "g1", group_deleted: true }));
      return new Response(JSON.stringify({ id: "g1", status: "open", occurrence_count: 1, representative: { event_id: "e1", question: "q" }, project: { id: "p1", name: "Support" }, event_type: "knowledge_gap", notes: [], tags: [] }));
    }) as typeof fetch;
    await act(async () => { root.render(<GroupDetail groupId="g1" navigate={navigate} />); await new Promise(resolve => setTimeout(resolve, 0)); });
    await act(async () => { Array.from(container.querySelectorAll("button")).find(button => button.textContent === "Delete occurrence")!.click(); });
    await act(async () => { (container.querySelector('[role="dialog"] button.danger-button') as HTMLButtonElement).click(); });
    expect(requests.find(request => request.url.endsWith("/admin/feedback/e1"))?.init).toMatchObject({ method: "DELETE" });
    expect(navigate).toHaveBeenCalledWith("/inbox");
  });
});
