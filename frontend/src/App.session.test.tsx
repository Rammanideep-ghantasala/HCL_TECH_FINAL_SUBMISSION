import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SESSION_EXPLANATION } from "./constants";
import { FEEDBACK_KEY, RECENT_KEY } from "./storage/session";
import { makeCard, makeInsufficientInput, makeResponse } from "./test/fixtures";
import { installFakeApi, renderApp, respond, searchButton, subjectInput } from "./test/harness";

function stored(key: string): Array<Record<string, unknown>> {
  const raw = window.sessionStorage.getItem(key);
  return raw === null ? [] : (JSON.parse(raw) as Array<Record<string, unknown>>);
}

/** Captures what the export would have downloaded instead of navigating. */
function captureDownloads() {
  const blobs: Blob[] = [];
  const createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob);
    return "blob:test";
  });
  // jsdom does not provide these two functions, so the test supplies them.
  Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  return { blobs, click };
}

function readBlob(blob: Blob | undefined): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!blob) {
      reject(new Error("Nothing was exported."));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("The export could not be read."));
    reader.readAsText(blob);
  });
}

describe("feedback kept in this tab", () => {
  it("records one verdict per result, exports it as JSON, and clears on request", async () => {
    const api = installFakeApi();
    const downloads = captureDownloads();
    const user = userEvent.setup();
    await renderApp();

    expect(screen.getByText(SESSION_EXPLANATION)).toBeInTheDocument();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    const related = makeCard({
      ticket: { ticket_id: "T-9002" },
      card: { result_id: "res-rel", rank: 2, match_level: "related", match_label: "Related context" },
    });
    await respond(api.searchCalls[0], 200, makeResponse({ related: [related] }));

    const article = screen.getAllByRole("article")[0];
    if (!article) {
      throw new Error("No result card was rendered.");
    }
    const useful = within(article).getByRole("button", { name: "Useful" });
    const notUseful = within(article).getByRole("button", { name: "Not useful" });
    expect(useful).toHaveAttribute("aria-pressed", "false");

    await user.click(useful);
    expect(useful).toHaveAttribute("aria-pressed", "true");
    expect(stored(FEEDBACK_KEY)).toHaveLength(1);
    expect(stored(FEEDBACK_KEY)[0]).toMatchObject({
      request_id: "req-test-1",
      query_fingerprint: "fp-test-1",
      result_id: "res-T-9001",
      ticket_id: "T-9001",
      rank: 1,
      match_level: "useful",
      verdict: "useful",
      reason: null,
      results_shown: ["T-9001", "T-9002"],
      status: "useful_matches",
      mode: "hybrid",
      degraded: false,
      index_version: "idx-test-1",
      relevance_config_version: "rel-test-1",
      model_id: "test-model",
    });
    expect(stored(FEEDBACK_KEY)[0]?.event_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(Number.isNaN(Date.parse(String(stored(FEEDBACK_KEY)[0]?.time)))).toBe(false);

    await user.click(notUseful);
    await user.selectOptions(within(article).getByLabelText("Reason (optional)"), "Outdated fix");
    expect(stored(FEEDBACK_KEY)).toHaveLength(1);
    expect(stored(FEEDBACK_KEY)[0]).toMatchObject({
      result_id: "res-T-9001",
      verdict: "not_useful",
      reason: "outdated_fix",
    });

    await user.click(screen.getByRole("button", { name: "Export feedback (JSON)" }));
    expect(downloads.click).toHaveBeenCalledTimes(1);
    expect(downloads.blobs[0]?.type).toBe("application/json");
    const plain = JSON.parse(await readBlob(downloads.blobs[0])) as {
      includes_searched_text: boolean;
      records: Array<Record<string, unknown>>;
    };
    expect(plain.includes_searched_text).toBe(false);
    expect(plain.records).toHaveLength(1);
    expect(plain.records[0]).toMatchObject({ ticket_id: "T-9001", verdict: "not_useful" });
    expect(plain.records[0]).not.toHaveProperty("searched_input");
    expect(JSON.stringify(plain)).not.toContain("Alpha problem");

    const include = screen.getByLabelText("Include the searched ticket text in the export");
    expect(include).not.toBeChecked();
    await user.click(include);
    await user.click(screen.getByRole("button", { name: "Export feedback (JSON)" }));
    const withText = JSON.parse(await readBlob(downloads.blobs[1])) as {
      records: Array<{ searched_input?: { subject?: string } }>;
    };
    expect(withText.records[0]?.searched_input?.subject).toBe("Alpha problem");

    expect(stored(RECENT_KEY)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Clear session data" }));
    expect(window.sessionStorage.getItem(FEEDBACK_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(RECENT_KEY)).toBeNull();
    expect(notUseful).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("No searches yet in this tab.")).toBeInTheDocument();
    // Clearing session data leaves the results on screen (one useful card, one related card).
    expect(screen.getAllByRole("article")).toHaveLength(2);
  });
});

describe("recent searches kept in this tab", () => {
  it("stores inputs only, keeps at most five, and fills the form without searching", async () => {
    const api = installFakeApi({
      autoRespond: () => ({ status: 200, body: makeResponse() }),
    });
    const user = userEvent.setup();
    await renderApp();

    for (let index = 1; index <= 6; index += 1) {
      fireEvent.change(subjectInput(), { target: { value: `Query ${index}` } });
      await user.click(searchButton());
      await screen.findByText(`Query ${index}`, { selector: "strong" });
    }
    expect(api.searchCalls).toHaveLength(6);

    const recent = stored(RECENT_KEY);
    expect(recent).toHaveLength(5);
    expect(recent.map((entry) => entry.subject)).toEqual([
      "Query 6",
      "Query 5",
      "Query 4",
      "Query 3",
      "Query 2",
    ]);
    for (const entry of recent) {
      expect(Object.keys(entry).sort()).toEqual([
        "category",
        "description",
        "error_detail",
        "outcome",
        "product",
        "subject",
        "time",
        "top_k",
      ]);
      expect(entry.outcome).toBe("useful_matches");
    }
    expect(window.sessionStorage.getItem(RECENT_KEY)).not.toContain("resolution");

    const list = screen.getByRole("heading", { name: "Recent in this tab" }).nextElementSibling;
    if (!(list instanceof HTMLElement)) {
      throw new Error("The recent list was not rendered.");
    }
    expect(within(list).getAllByRole("button")).toHaveLength(5);
    await user.click(within(list).getByRole("button", { name: /Query 3/ }));
    expect(subjectInput()).toHaveValue("Query 3");
    expect(api.searchCalls).toHaveLength(6);
  });

  it("keeps searching when browser storage is blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const api = installFakeApi({
      autoRespond: () => ({ status: 200, body: makeInsufficientInput() }),
    });
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Help");
    await user.click(searchButton());

    expect(await screen.findByText("This ticket is too short to search reliably.")).toBeInTheDocument();
    expect(api.searchCalls).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Help/ })).toBeInTheDocument();
  });
});
