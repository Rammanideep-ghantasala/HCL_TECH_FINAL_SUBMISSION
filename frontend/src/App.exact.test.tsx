import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ExactMatchInfo, ResultCard } from "./api/types";
import { EXACT_MATCH_CAUTION } from "./constants";
import { makeCard, makeResponse, makeRetrieval, makeTicket, segmentsOf } from "./test/fixtures";
import { installFakeApi, renderApp, respond, searchButton, subjectInput } from "./test/harness";

const SUBJECT = "Widget sync stops after update";

function exactCard(ticketId: string, resolution: string, rank: number): ResultCard {
  const ticket = makeTicket({ ticket_id: ticketId, subject: SUBJECT, resolution_notes: resolution });
  return makeCard({
    ticket,
    card: {
      result_id: `res-${ticketId}`,
      rank,
      match_level: "exact",
      match_label: "Exact text match",
      evidence: {
        found_by: ["exact_text"],
        keyword_rank: null,
        semantic_rank: null,
        shared_terms: ["sync"],
        new_subject_segments: segmentsOf(SUBJECT, "sync"),
        new_description_segments: segmentsOf(ticket.description, "sync"),
        past_subject_segments: segmentsOf(ticket.subject, "sync"),
        past_description_segments: segmentsOf(ticket.description, "sync"),
        excerpts: [],
        retrieval_note: "Exact text match: identical wording. It does not show that this is the same incident.",
      },
      differences: { only_in_new: [], only_in_past: [], unseen_in_history: [], comparison_rows: [] },
      scores: { keyword_cosine: null, semantic_cosine: null, query_coverage: null, rrf: null },
    },
  });
}

function exactInfo(overrides: Partial<ExactMatchInfo> = {}): ExactMatchInfo {
  return {
    ticket_id: null,
    ticket_id_status: "not_supplied",
    matched_ticket_ids: [],
    message: null,
    ...overrides,
  };
}

async function searchAndAnswer(api: ReturnType<typeof installFakeApi>, answer: ReturnType<typeof makeResponse>) {
  const user = userEvent.setup();
  await renderApp();
  await user.type(subjectInput(), SUBJECT);
  await user.click(searchButton());
  await respond(api.searchCalls[0], 200, answer);
  return user;
}

describe("exact text matches", () => {
  it("labels exact matches, keeps each ticket separate and does not claim the same incident", async () => {
    const api = installFakeApi();
    const cards = [exactCard("T-9001", "Resolution one.", 1), exactCard("T-9002", "Resolution two.", 2)];
    await searchAndAnswer(
      api,
      makeResponse({
        message: "Exact text match found: identical to 2 past tickets.",
        results: cards,
        exact_match: exactInfo({ matched_ticket_ids: ["T-9001", "T-9002"] }),
        retrieval: makeRetrieval({ similarity_search_run: false }),
        timing_ms: { total: 1, keyword: null, semantic: null },
      }),
    );

    expect(screen.getByText("Exact text match found")).toBeInTheDocument();
    expect(screen.getByText("T-9001, T-9002")).toBeInTheDocument();
    expect(screen.getByText(EXACT_MATCH_CAUTION)).toBeInTheDocument();
    expect(screen.getByText("No similarity search was run for this ticket.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Exact text match found: 2 past tickets with identical wording shown.",
    );

    const articles = screen.getAllByRole("article");
    expect(articles).toHaveLength(2);
    // Each resolution stays traceable to its own source ticket.
    for (const [index, article] of articles.entries()) {
      const id = index === 0 ? "T-9001" : "T-9002";
      expect(within(article).getByText("Exact text match")).toBeInTheDocument();
      expect(
        within(article).getByRole("heading", { name: `Original resolution (verbatim from ${id})` }),
      ).toBeInTheDocument();
      expect(within(article).getByText(index === 0 ? "Resolution one." : "Resolution two.")).toBeInTheDocument();
      expect(within(article).getByText("exact text comparison")).toBeInTheDocument();
    }
    expect(document.body.textContent).not.toMatch(/same ticket as|is the same incident as|duplicate of/i);
  });

  it("warns when a supplied ticket id already exists with different text and offers the stored version", async () => {
    const stored = makeTicket({ ticket_id: "T-9001", resolution_notes: "Stored resolution of T-9001." });
    const api = installFakeApi({ tickets: [stored] });
    const user = await searchAndAnswer(
      api,
      makeResponse({
        exact_match: exactInfo({
          ticket_id: "T-9001",
          ticket_id_status: "exists_different_text",
          message: "Ticket T-9001 already exists with a different subject or description. Nothing was saved or changed.",
        }),
      }),
    );

    expect(screen.getByText("T-9001 already exists with different text")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was saved or changed/)).toBeInTheDocument();
    expect(screen.queryByText("Exact text match found")).not.toBeInTheDocument();

    const view = screen.getByRole("button", { name: "View T-9001" });
    await user.click(view);
    const panel = document.getElementById(view.getAttribute("aria-controls") ?? "");
    if (!panel) {
      throw new Error("The stored-version panel is missing");
    }
    expect(await within(panel).findByText(stored.resolution_notes, { exact: false })).toBeInTheDocument();
    expect(api.ticketRequests).toContain("T-9001");
  });

  it("shows nothing extra for an ordinary similarity result or an older response without exact_match", async () => {
    const api = installFakeApi();
    await searchAndAnswer(api, makeResponse());
    expect(screen.queryByText("Exact text match found")).not.toBeInTheDocument();
    expect(screen.queryByText("Ticket ID", { exact: false })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Search finished. 1 useful match shown.");
  });
});
