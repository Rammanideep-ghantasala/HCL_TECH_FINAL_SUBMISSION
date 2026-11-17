import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SummaryResponse } from "./api/types";
import { SUMMARY_TIMEOUT_MS } from "./components/SummaryPanel";
import { makeApiError, makeCard, makeResponse, metaFixture } from "./test/fixtures";
import { installFakeApi, renderApp, respond, type FakeApi } from "./test/harness";

function result(count: number, requestId = "results-1") {
  return makeResponse({
    request_id: requestId, summary_token: `signed-${requestId}`, top_k: count > 3 ? 5 : 3,
    results: Array.from({ length: count }, (_, i) => makeCard({
      ticket: { ticket_id: `T-${i + 1}`, subject: `Historical ticket ${i + 1}` },
      card: { result_id: `${requestId}-${i}`, rank: i + 1 },
    })),
    related: [makeCard({ ticket: { ticket_id: "RELATED-1" }, card: { match_level: "related" } })],
  });
}

function summary(count: number, text = "Recorded historical issues."): SummaryResponse {
  const ids = Array.from({ length: count }, (_, i) => `T-${i + 1}`);
  return {
    ticket_ids: ids, source_revisions: ids.map(() => "rev"), model: "test-model",
    prompt_version: "test", generated_at: "2026-11-17T10:00:00Z",
    disclaimer: "AI-generated summary — verify against the original tickets.",
    summary: {
      overview: [{ text, ticket_ids: ids }],
      tickets: ids.map((id) => ({ ticket_id: id, reported_issue: `Reported issue ${id}`,
        recorded_resolution: `Documented workaround ${id}`, applicability_notes: ["Cause not recorded."] })),
    },
  };
}

async function startSearch(api: FakeApi, count: 3 | 5 = 3, returned: number = count) {
  const user = userEvent.setup();
  await renderApp();
  await user.selectOptions(screen.getByLabelText("Results to show"), String(count));
  expect(api.searchCalls).toHaveLength(0);
  await user.type(screen.getByLabelText("Subject"), "Invoice total is wrong");
  await user.click(screen.getByRole("button", { name: "Search" }));
  await respond(api.searchCalls[0], 200, result(returned));
  return user;
}

describe("selectable matches and explicit summaries", () => {
  it.each([3, 5] as const)("summarizes exactly the %i displayed main matches only after a click", async (count) => {
    const api = installFakeApi();
    const user = await startSearch(api, count);
    expect(api.searchCalls[0]?.body.top_k).toBe(count);
    expect(api.summaryCalls).toHaveLength(0);
    const button = screen.getByRole("button", { name: `Summarize ${count} tickets` });
    await user.click(button);
    await user.click(button);
    expect(api.summaryCalls).toHaveLength(1);
    expect(api.summaryCalls[0]?.body).toEqual({
      summary_token: "signed-results-1", ticket_ids: Array.from({ length: count }, (_, i) => `T-${i + 1}`),
    });
    await respond(api.summaryCalls[0], 200, summary(count));
    expect(screen.getByRole("heading", { name: `Summary of ${count} displayed tickets` })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "T-1 — original ticket" })).toHaveAttribute("href", "#ticket-result-T-1");
    expect(api.searchCalls).toHaveLength(1);
  });

  it("summarizes only two when Top 5 has just two qualifying matches", async () => {
    const api = installFakeApi();
    const user = await startSearch(api, 5, 2);
    expect(screen.getByText(/Only 2 matching tickets qualify/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Summarize 2 tickets" }));
    await respond(api.summaryCalls[0], 200, summary(2));
    expect(screen.getByRole("heading", { name: "Summary of 2 displayed tickets" })).toBeInTheDocument();
  });

  it("reruns after a count change and ignores a late summary for Top 5", async () => {
    const api = installFakeApi({ rejectOnAbort: false });
    const user = await startSearch(api, 5);
    await user.click(screen.getByRole("button", { name: "Summarize 5 tickets" }));
    await user.selectOptions(screen.getByLabelText("Results to show"), "3");
    expect(api.summaryCalls[0]?.signal?.aborted).toBe(true);
    expect(api.searchCalls[1]?.body.top_k).toBe(3);
    await respond(api.searchCalls[1], 200, result(3, "new-results"));
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    await respond(api.summaryCalls[1], 200, summary(3, "Current three-ticket summary."));
    await respond(api.summaryCalls[0], 200, summary(5, "Obsolete five-ticket summary."));
    expect(screen.getByText("Current three-ticket summary.")).toBeInTheDocument();
    expect(screen.queryByText("Obsolete five-ticket summary.")).not.toBeInTheDocument();
  });

  it("ignores an older Top 3 search after switching to Top 5 while loading", async () => {
    const api = installFakeApi({ rejectOnAbort: false });
    const user = userEvent.setup();
    await renderApp();
    expect(screen.getByLabelText("Results to show")).toHaveValue("3");
    await user.type(screen.getByLabelText("Subject"), "Invoice total is wrong");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.selectOptions(screen.getByLabelText("Results to show"), "5");
    await respond(api.searchCalls[1], 200, result(5, "latest"));
    await respond(api.searchCalls[0], 200, result(3, "old"));
    expect(screen.getByRole("button", { name: "Summarize 5 tickets" })).toBeInTheDocument();
    expect(api.searchCalls[0]?.signal?.aborted).toBe(true);
  });

  it.each(["query", "category", "clear"])("invalidates summaries on %s changes", async (edit) => {
    const api = installFakeApi({ rejectOnAbort: false });
    const user = await startSearch(api);
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    if (edit === "query") await user.type(screen.getByLabelText("Subject"), " changed");
    if (edit === "category") await user.selectOptions(screen.getByLabelText("Category filter"), "Billing");
    if (edit === "clear") await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(api.summaryCalls[0]?.signal?.aborted).toBe(true);
    await respond(api.summaryCalls[0], 200, summary(3, "Stale answer."));
    expect(screen.queryByText("Stale answer.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Summarize \d/ })).not.toBeInTheDocument();
  });

  it("clears an already completed summary on count changes without automatically summarizing again", async () => {
    const api = installFakeApi();
    const user = await startSearch(api, 5);
    await user.click(screen.getByRole("button", { name: "Summarize 5 tickets" }));
    await respond(api.summaryCalls[0], 200, summary(5));
    await user.selectOptions(screen.getByLabelText("Results to show"), "3");
    expect(screen.queryByRole("heading", { name: "Summary of 5 displayed tickets" })).not.toBeInTheDocument();
    await respond(api.searchCalls[1], 200, result(3, "updated"));
    expect(api.summaryCalls).toHaveLength(1);
  });

  it("keeps matches after provider failure and permits an explicit retry", async () => {
    const api = installFakeApi();
    const user = await startSearch(api);
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    await respond(api.summaryCalls[0], 504, makeApiError("summary_timeout", "Provider timed out. Please retry."));
    expect(screen.getByRole("alert")).toHaveTextContent("Provider timed out.");
    expect(document.getElementById("ticket-result-T-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry summary" }));
    await respond(api.summaryCalls[1], 200, summary(3));
    expect(api.searchCalls).toHaveLength(1);
  });

  it("requires refreshing changed source records", async () => {
    const api = installFakeApi();
    const user = await startSearch(api);
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    await respond(api.summaryCalls[0], 409, makeApiError("summary_refresh_required", "Search again before summarizing."));
    expect(screen.getByRole("button", { name: "Retry summary" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Search again");
  });

  it("shows unconfigured summaries without disabling search", async () => {
    const api = installFakeApi({ meta: { ...metaFixture, summarization: {
      configured: false, provider: "openai", model: "test", data_notice: "Clicking sends these sources to OpenAI.",
    } } });
    await startSearch(api);
    expect(screen.getByRole("button", { name: "Summarize 3 tickets" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Search" })).not.toBeDisabled();
    expect(api.summaryCalls).toHaveLength(0);
  });

  it("rejects a response containing tickets outside the displayed list", async () => {
    const api = installFakeApi();
    const user = await startSearch(api);
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    await respond(api.summaryCalls[0], 200, summary(5));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Summary of/ })).not.toBeInTheDocument();
  });

  it("renders provider text as text, with no executable markup", async () => {
    const api = installFakeApi();
    const user = await startSearch(api);
    await user.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    const text = '<img src=x onerror="alert(1)">';
    await respond(api.summaryCalls[0], 200, summary(3, text));
    expect(screen.getByText(text, { exact: false })).toBeInTheDocument();
    expect(document.querySelector(".summary-content img")).toBeNull();
  });

  it("times out even when transport ignores abort, and ignores its late result", async () => {
    const api = installFakeApi({ rejectOnAbort: false });
    await startSearch(api);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Summarize 3 tickets" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(SUMMARY_TIMEOUT_MS); });
    expect(screen.getByRole("alert")).toHaveTextContent("timed out");
    await act(async () => { api.summaryCalls[0]?.respond(200, summary(3)); });
    expect(screen.queryByRole("heading", { name: /Summary of/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry summary" })).toBeEnabled();
  });
});
