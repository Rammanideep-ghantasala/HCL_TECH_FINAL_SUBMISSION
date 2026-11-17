import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { SEARCH_TIMEOUT_MS } from "./constants";
import {
  examplesFixture,
  makeApiError,
  makeCard,
  makeInsufficientInput,
  makeNoStrongMatch,
  makeResponse,
  makeRetrieval,
  metaFixture,
} from "./test/fixtures";
import { installFakeApi, renderApp, respond, searchButton, subjectInput } from "./test/harness";

function cardHeadings(): string[] {
  return screen
    .queryAllByRole("article")
    .map((article) => within(article).getByRole("heading", { level: 3 }).textContent ?? "");
}

describe("search requests", () => {
  it("shows the latest search even when an older answer arrives afterwards", async () => {
    const api = installFakeApi({ rejectOnAbort: false });
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await user.clear(subjectInput());
    await user.type(subjectInput(), "Beta problem");
    await user.click(searchButton());

    expect(api.searchCalls).toHaveLength(2);
    expect(api.searchCalls[0]?.signal?.aborted).toBe(true);

    const answerA = makeResponse({
      request_id: "req-a",
      results: [makeCard({ ticket: { ticket_id: "T-9001", subject: "Alpha past ticket" } })],
    });
    const answerB = makeResponse({
      request_id: "req-b",
      results: [makeCard({ ticket: { ticket_id: "T-9002", subject: "Beta past ticket" } })],
    });

    await respond(api.searchCalls[1], 200, answerB);
    await respond(api.searchCalls[0], 200, answerA);

    expect(cardHeadings()).toHaveLength(1);
    expect(cardHeadings()[0]).toContain("T-9002");
    expect(screen.queryByText("Alpha past ticket")).not.toBeInTheDocument();
    expect(screen.getByText("Beta problem", { selector: "strong" })).toBeInTheDocument();
  });

  it("does not render an answer that arrives after Clear", async () => {
    const api = installFakeApi({ rejectOnAbort: false });
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    expect(screen.getByText("Searching past tickets…", { selector: "p" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(api.searchCalls[0]?.signal?.aborted).toBe(true);
    await respond(api.searchCalls[0], 200, makeResponse());

    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Before you search" })).toBeInTheDocument();
    expect(subjectInput()).toHaveValue("");
  });

  it("sends one request when the same ticket is submitted again while in flight", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await user.click(searchButton());
    await user.type(subjectInput(), "{Enter}");

    expect(api.searchCalls).toHaveLength(1);
    expect(searchButton()).toHaveAttribute("aria-disabled", "true");
    expect(searchButton()).not.toBeDisabled();

    await respond(api.searchCalls[0], 200, makeResponse());
    expect(searchButton()).not.toHaveAttribute("aria-disabled");
  });

  it("stops a search that takes too long and offers Retry", async () => {
    vi.useFakeTimers();
    const api = installFakeApi();
    render(<App />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    fireEvent.change(subjectInput(), { target: { value: "Slow problem" } });
    fireEvent.click(searchButton());
    expect(api.searchCalls).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SEARCH_TIMEOUT_MS - 1);
    });
    expect(screen.queryByText("The search timed out")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByText("The search timed out")).toBeInTheDocument();
    expect(api.searchCalls[0]?.signal?.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(subjectInput()).toHaveValue("Slow problem");
  });
});

describe("outcomes", () => {
  it("renders a failure and an honest no-match as visibly different states, with one request per Retry", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await respond(
      api.searchCalls[0],
      503,
      makeApiError("busy", "The search service is busy. Try again shortly."),
    );

    expect(screen.getByRole("heading", { name: "Search did not complete" })).toBeInTheDocument();
    expect(screen.getByText("Service problem")).toBeInTheDocument();
    expect(screen.getByText("The search service is busy. Try again shortly.")).toBeInTheDocument();
    expect(screen.getByText("req-err-7")).toBeInTheDocument();
    expect(screen.queryByText("No relevant historical match found.")).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="mailto:"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "Record resolution" })).not.toBeInTheDocument();
    expect(subjectInput()).toHaveValue("Alpha problem");

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.searchCalls).toHaveLength(2);
    await respond(api.searchCalls[1], 500, makeApiError("internal_error", "Unexpected error."));

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.searchCalls).toHaveLength(3);
    expect(api.searchCalls[2]?.body).toEqual(api.searchCalls[0]?.body);
    await respond(api.searchCalls[2], 200, makeNoStrongMatch());

    expect(screen.getByRole("heading", { name: "Search results" })).toBeInTheDocument();
    expect(screen.getByText("No relevant historical match found.")).toBeInTheDocument();
    expect(screen.getByText("No previous matching tickets are available for this search.")).toBeInTheDocument();
    expect(
      screen.getByText("The search ran normally. This is a search result, not an error."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Not found in any of the 40 past tickets: zorblat/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record resolution" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      /^Search finished\. No relevant historical match found\./,
    );
    expect(screen.queryByText("Service problem")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(api.searchCalls).toHaveLength(3);
  });

  it("treats an unrecognised status as a failure, not a blank screen", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await respond(api.searchCalls[0], 200, { ...makeResponse(), status: "something_new" });

    expect(screen.getByRole("heading", { name: "Search did not complete" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
  });

  it("shows the keyword-only banner when retrieval is degraded", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await respond(
      api.searchCalls[0],
      200,
      makeResponse({
        retrieval: makeRetrieval({
          mode: "keyword_only",
          degraded: true,
          degraded_reason: "semantic model not loaded",
        }),
      }),
    );

    expect(
      screen.getByText("Keyword-only results: meaning-based search is unavailable right now"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(1);
  });

  it("shows suggestions and no cards for insufficient input, and Go to field focuses the field", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Help");
    await user.click(searchButton());
    await respond(api.searchCalls[0], 200, makeInsufficientInput());

    expect(screen.getByText("This ticket is too short to search reliably.")).toBeInTheDocument();
    expect(screen.getByText("Add the exact error code if the customer gave one.")).toBeInTheDocument();
    expect(
      screen.getByText("Based on: no error code was found in the new ticket"),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("article")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Go to field: Error code or message" }));
    expect(screen.getByLabelText("Error code or message")).toHaveFocus();
  });

  it("keeps related tickets collapsed and names the active filter with a one-click way out", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.selectOptions(screen.getByLabelText("Category filter"), "Billing");
    await user.click(searchButton());
    expect(api.searchCalls[0]?.body.category).toBe("Billing");

    await respond(
      api.searchCalls[0],
      200,
      makeResponse({
        related: [
          makeCard({
            ticket: { ticket_id: "T-9005", subject: "Loosely related past ticket" },
            card: { result_id: "res-rel", rank: 2, match_level: "related", match_label: "Related context" },
          }),
        ],
        retrieval: makeRetrieval({ category_filter: "Billing", hidden_by_filter: 2 }),
      }),
    );

    expect(screen.getByText("Category filter active: Billing")).toBeInTheDocument();
    expect(
      screen.getByText(
        "2 potentially useful tickets in other categories are hidden by this filter.",
      ),
    ).toBeInTheDocument();
    const related = screen.getByText("Related context (below the match threshold)").closest("details");
    expect(related).not.toBeNull();
    expect(related).not.toHaveAttribute("open");

    await user.click(screen.getByRole("button", { name: "Clear filter and search again" }));
    expect(api.searchCalls).toHaveLength(2);
    expect(api.searchCalls[1]?.body.category).toBeNull();
    expect(api.searchCalls[1]?.body.subject).toBe("Alpha problem");
    expect(screen.getByLabelText("Category filter")).toHaveValue("");
  });

  it("renders results from the submitted snapshot and notes when the form was edited", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await respond(api.searchCalls[0], 200, makeResponse());

    expect(screen.getByRole("heading", { name: "Search results" })).toHaveFocus();
    expect(screen.queryByText("The form has been edited since this search.")).not.toBeInTheDocument();

    await user.type(subjectInput(), " changed");
    expect(screen.getByText("The form has been edited since this search.")).toBeInTheDocument();
    expect(screen.getByText("Alpha problem", { selector: "strong" })).toBeInTheDocument();
    expect(api.searchCalls).toHaveLength(1);
  });
});

describe("validation", () => {
  it("shows a message and sends nothing when subject and description are empty", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(screen.getByLabelText("Description"), "   ");
    await user.click(searchButton());

    expect(
      screen.getByText("Enter a subject or a description of the problem. Both are empty."),
    ).toBeInTheDocument();
    expect(api.searchCalls).toHaveLength(0);
    expect(subjectInput()).toHaveFocus();
    expect(subjectInput()).toHaveAttribute("aria-invalid", "true");
  });

  it("blocks a field that is over its limit", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    const tooLong = "x".repeat(metaFixture.limits.subject_max_chars + 3);
    fireEvent.change(subjectInput(), { target: { value: tooLong } });
    await user.click(searchButton());

    expect(api.searchCalls).toHaveLength(0);
    expect(subjectInput()).toHaveAccessibleDescription(
      expect.stringContaining("3 characters over the limit of 40"),
    );
  });

  it("attaches a server 422 detail to its field", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    await user.type(subjectInput(), "Alpha problem");
    await user.type(screen.getByLabelText("Description"), "Some description");
    await user.click(searchButton());
    await respond(
      api.searchCalls[0],
      422,
      makeApiError("validation_error", "The ticket could not be accepted.", [
        { field: "description", message: "Description must be shorter than the server allows." },
      ]),
    );

    const description = screen.getByLabelText("Description");
    expect(description).toHaveAttribute("aria-invalid", "true");
    expect(description).toHaveAccessibleDescription(
      expect.stringContaining("Description must be shorter than the server allows."),
    );
    expect(description).toHaveFocus();
    expect(subjectInput()).not.toHaveAttribute("aria-invalid");
    expect(description).toHaveValue("Some description");
  });
});

describe("start-up requests", () => {
  it("shows the server notice and fills the form from an example without searching", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();

    expect(screen.getByText(metaFixture.data_notice)).toBeInTheDocument();
    const example = examplesFixture.examples[1];
    await user.click(screen.getByRole("button", { name: new RegExp(example?.subject ?? "missing") }));

    expect(subjectInput()).toHaveValue(example?.subject);
    expect(screen.getByLabelText("Description")).toHaveValue(example?.description);
    expect(api.searchCalls).toHaveLength(0);
  });

  it("keeps working with built-in limits when settings and examples cannot be loaded", async () => {
    const api = installFakeApi({ metaStatus: 500, examplesStatus: 500 });
    const user = userEvent.setup();
    await renderApp();

    expect(screen.getByText(/Settings could not be loaded from the server/)).toBeInTheDocument();
    expect(screen.queryByText(/Load an example/)).not.toBeInTheDocument();
    expect(screen.getByText(/Do not paste real customer data/)).toBeInTheDocument();

    await user.type(subjectInput(), "Alpha problem");
    await user.click(searchButton());
    await respond(api.searchCalls[0], 200, makeResponse());
    expect(screen.getAllByRole("article")).toHaveLength(1);
  });
});
