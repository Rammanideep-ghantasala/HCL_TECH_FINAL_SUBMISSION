import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SAVED_AND_SEARCHABLE } from "./components/ResolutionForm";
import { ESCALATION_EMAIL_SUBJECT, FALLBACK_ESCALATION_EMAIL } from "./constants";
import {
  makeApiError,
  makeCard,
  makeFailedIndexing,
  makeInsufficientInput,
  makeNoStrongMatch,
  makeRefreshResponse,
  makeRetrieval,
  makeSaveResponse,
  makeStoredTicket,
  metaFixture,
} from "./test/fixtures";
import {
  failNetwork,
  installFakeApi,
  renderApp,
  respond,
  searchButton,
  subjectInput,
  type FakeApi,
} from "./test/harness";

type User = ReturnType<typeof userEvent.setup>;

const SUBJECT = "Zorblat printer meltdown";
const DESCRIPTION = "The quantum spooler emits purple smoke after the update.";
const INVESTIGATION = "Checked the spooler log and found a stuck job.";
const STEPS = "Cleared the stuck job and restarted the spooler.";
const VERIFICATION = "Printed a test page with the customer.";
const EXPECTED_MAILTO = `mailto:${FALLBACK_ESCALATION_EMAIL}?subject=${encodeURIComponent(ESCALATION_EMAIL_SUBJECT)}`;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** A resolution-form control. Its labels differ from the search form's ("Subject" there). */
function field(label: string): HTMLElement {
  return screen.getByLabelText(label);
}

function saveButton(): HTMLElement {
  return screen.getByRole("button", { name: "Save resolved ticket" });
}

function mailtoLinks(): Element[] {
  return Array.from(document.querySelectorAll('a[href^="mailto:"]'));
}

/**
 * Sets a field's value with one change event. Typing key by key re-renders
 * the whole page per character, which is slow and is not what these tests
 * are about. Buttons are always pressed with user-event clicks.
 */
function enter(element: HTMLElement, text: string) {
  fireEvent.change(element, { target: { value: text } });
}

async function searchFor(api: FakeApi, user: User, answer: unknown, status = 200) {
  await renderApp();
  enter(subjectInput(), SUBJECT);
  enter(screen.getByLabelText("Description"), DESCRIPTION);
  await user.click(searchButton());
  await respond(api.searchCalls[0], status, answer);
}

async function openForm(user: User) {
  await user.click(screen.getByRole("button", { name: "Record resolution" }));
}

async function fillRequired(user: User, options: { resolvedBy?: boolean; confirm?: boolean } = {}) {
  enter(field("Category (required)"), "Printing");
  enter(field("Investigation approach (required)"), INVESTIGATION);
  enter(field("Resolution steps (required)"), STEPS);
  enter(field("Verification (required)"), VERIFICATION);
  if (options.resolvedBy ?? true) {
    enter(field("Resolved by (required)"), "Agent Nine");
  }
  if (options.confirm ?? true) {
    await user.click(field("I confirm this issue has been resolved (required)"));
  }
}

/** Searches, gets no match, opens the form and fills every required field. */
async function readyToSave(api: FakeApi, user: User) {
  await searchFor(api, user, makeNoStrongMatch());
  await openForm(user);
  await fillRequired(user);
}

describe("no relevant historical match", () => {
  it("shows the three sentences, the escalation link and Record resolution, and no previous-ticket cards", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    const related = makeCard({
      ticket: { ticket_id: "T-9005", subject: "Loosely related invented ticket" },
      card: { result_id: "res-rel", rank: 1, match_level: "related", match_label: "Related context" },
    });
    await searchFor(
      api,
      user,
      makeNoStrongMatch({
        related: [related],
        retrieval: makeRetrieval({
          mode: "keyword_only",
          degraded: true,
          degraded_reason: "semantic model not loaded",
          category_filter: "Billing",
          hidden_by_filter: 1,
        }),
      }),
    );

    expect(screen.getByText("No relevant historical match found.")).toBeInTheDocument();
    expect(
      screen.getByText("No previous matching tickets are available for this search."),
    ).toBeInTheDocument();
    expect(screen.getByText("Human investigation is required.")).toBeInTheDocument();

    const link = screen.getByRole("link", { name: FALLBACK_ESCALATION_EMAIL });
    expect(link).toHaveAttribute("href", EXPECTED_MAILTO);
    expect(
      screen.getByText(
        /After you solve the issue, record your investigation approach and the resolution so this ticket can help future searches\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record resolution" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    // No cards at all, not even the below-threshold ones the server sent.
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByText(/T-9005/)).not.toBeInTheDocument();
    expect(screen.queryByText("Loosely related invented ticket")).not.toBeInTheDocument();
    expect(screen.queryByText(/Closest tickets/)).not.toBeInTheDocument();

    // The reduced-search and filter notices still appear as before.
    expect(
      screen.getByText("Keyword-only results: meaning-based search is unavailable right now"),
    ).toBeInTheDocument();
    expect(screen.getByText("Category filter active: Billing")).toBeInTheDocument();

    expect(screen.getByRole("status")).toHaveTextContent(
      /^Search finished\. No relevant historical match found\..*not an error\.$/,
    );
    // Nothing happens on its own: no further search, no save.
    expect(api.searchCalls).toHaveLength(1);
    expect(api.saveCalls).toHaveLength(0);
    expect(api.fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    ["help.desk@example.org", "help.desk@example.org"],
    ["help@example.org?body=leak", FALLBACK_ESCALATION_EMAIL],
  ])("links the server address %s as %s", async (sent, shown) => {
    const api = installFakeApi({ meta: { ...metaFixture, escalation_email: sent } });
    const user = userEvent.setup();
    await searchFor(api, user, makeNoStrongMatch());

    expect(screen.getByRole("link", { name: shown })).toHaveAttribute(
      "href",
      `mailto:${shown}?subject=${encodeURIComponent(ESCALATION_EMAIL_SUBJECT)}`,
    );
    expect(mailtoLinks()).toHaveLength(1);
  });

  it("never puts ticket text in the escalation link", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();
    const hostile = "Leak ?body=secret&cc=x@example.org";
    fireEvent.change(subjectInput(), { target: { value: hostile } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: DESCRIPTION } });
    await user.click(searchButton());
    await respond(api.searchCalls[0], 200, makeNoStrongMatch());
    await openForm(user);
    enter(field("Investigation approach (required)"), "Zorblat private notes");

    const links = mailtoLinks();
    expect(links).toHaveLength(1);
    const href = links[0]?.getAttribute("href") ?? "";
    expect(href).toBe(EXPECTED_MAILTO);
    for (const word of ["Leak", "secret", "cc=", "body", "Zorblat", "quantum", "spooler", "private"]) {
      expect(href).not.toContain(word);
      expect(decodeURIComponent(href)).not.toContain(word);
    }
  });

  it.each([
    [503, "search_unavailable"],
    [500, "internal_error"],
  ])("shows a %i service failure as a failure with none of the no-match text", async (status, code) => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await searchFor(api, user, makeApiError(code, "The invented search service failed."), status);

    expect(screen.getByRole("heading", { name: "Search did not complete" })).toBeInTheDocument();
    expect(screen.getByText("Service problem")).toBeInTheDocument();
    expect(screen.queryByText("No relevant historical match found.")).not.toBeInTheDocument();
    expect(
      screen.queryByText("No previous matching tickets are available for this search."),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Human investigation is required.")).not.toBeInTheDocument();
    expect(mailtoLinks()).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Record resolution" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent(/No relevant historical match/);
  });

  it("offers no escalation link or Record resolution for input that was too vague to search", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await searchFor(api, user, makeInsufficientInput());

    expect(screen.getByText("More detail is needed before searching")).toBeInTheDocument();
    expect(mailtoLinks()).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Record resolution" })).not.toBeInTheDocument();
    expect(screen.queryByText("No relevant historical match found.")).not.toBeInTheDocument();
  });
});

describe("resolution form", () => {
  it("is prefilled from the searched snapshot, not from later edits to the search form", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await renderApp();
    enter(subjectInput(), SUBJECT);
    enter(screen.getByLabelText("Description"), DESCRIPTION);
    enter(screen.getByLabelText("Product or system"), "Spooler 3");
    enter(screen.getByLabelText("Error code or message"), "SPL-42");
    await user.selectOptions(screen.getByLabelText("Category filter"), "Billing");
    await user.click(searchButton());
    await respond(
      api.searchCalls[0],
      200,
      makeNoStrongMatch({ retrieval: makeRetrieval({ category_filter: "Billing" }) }),
    );

    // Unsaved edits to the search form after the search ran.
    await user.clear(subjectInput());
    enter(subjectInput(), "Edited after the search");
    enter(screen.getByLabelText("Product or system"), "Spooler 4 edited");

    await openForm(user);
    expect(screen.getByRole("heading", { name: "Record the resolution" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Record resolution" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(field("Subject (required)")).toHaveValue(SUBJECT);
    expect(field("Description (required)")).toHaveValue(DESCRIPTION);
    expect(field("Product or version (optional)")).toHaveValue("Spooler 3");
    expect(field("Error details (optional)")).toHaveValue("SPL-42");
    expect(field("Category (required)")).toHaveValue("Billing");
    expect(field("Ticket ID (optional)")).toHaveValue("");
    expect(field("Ticket ID (optional)")).toHaveAccessibleDescription(
      expect.stringContaining("Leave blank to generate a new ID"),
    );
    expect(field("Resolved by (required)")).toHaveValue("");
    expect(field("I confirm this issue has been resolved (required)")).not.toBeChecked();

    enter(subjectInput(), "Edited again");
    expect(field("Subject (required)")).toHaveValue(SUBJECT);
    expect(api.searchCalls).toHaveLength(1);
  });

  it("keeps the draft across closing and reopening, and a new search closes it", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await searchFor(api, user, makeNoStrongMatch());
    await openForm(user);
    enter(field("Investigation approach (required)"), "Partial notes");

    await user.click(screen.getByRole("button", { name: "Close form" }));
    expect(screen.queryByRole("heading", { name: "Record the resolution" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record resolution" })).toHaveFocus();

    await openForm(user);
    expect(field("Investigation approach (required)")).toHaveValue("Partial notes");

    await user.click(searchButton());
    expect(screen.queryByRole("heading", { name: "Record the resolution" })).not.toBeInTheDocument();
    await respond(api.searchCalls[1], 200, makeNoStrongMatch({ request_id: "req-test-nsm-2" }));
    expect(screen.queryByRole("heading", { name: "Record the resolution" })).not.toBeInTheDocument();

    await openForm(user);
    expect(field("Investigation approach (required)")).toHaveValue("");
    expect(api.saveCalls).toHaveLength(0);
  });

  it("sends nothing and marks the fields when required fields are empty or unconfirmed", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await searchFor(api, user, makeNoStrongMatch());
    await openForm(user);

    await user.clear(field("Subject (required)"));
    await user.click(saveButton());

    expect(api.saveCalls).toHaveLength(0);
    for (const label of [
      "Subject (required)",
      "Category (required)",
      "Investigation approach (required)",
      "Resolution steps (required)",
      "Verification (required)",
      "Resolved by (required)",
    ]) {
      expect(field(label)).toHaveAttribute("aria-invalid", "true");
      expect(field(label)).toHaveAccessibleDescription(expect.stringContaining("is required"));
    }
    expect(field("Description (required)")).not.toHaveAttribute("aria-invalid");
    expect(field("Root cause (optional)")).not.toHaveAttribute("aria-invalid");
    const confirm = field("I confirm this issue has been resolved (required)");
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    expect(field("Subject (required)")).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent(/^Not saved\. Correct the marked fields/);

    enter(field("Subject (required)"), SUBJECT);
    await fillRequired(user, { confirm: false });
    await user.click(saveButton());

    expect(api.saveCalls).toHaveLength(0);
    expect(confirm).toHaveFocus();
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    expect(confirm).toHaveAccessibleDescription(expect.stringContaining("confirm the issue has been resolved"));
    expect(field("Investigation approach (required)")).not.toHaveAttribute("aria-invalid");
  });

  it("checks the ticket id, category and tags patterns before sending", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);
    enter(field("Ticket ID (optional)"), "-bad id");
    await user.clear(field("Category (required)"));
    enter(field("Category (required)"), "Printing!");
    enter(field("Tags (optional)"), "ok, not/ok");
    await user.click(saveButton());

    expect(api.saveCalls).toHaveLength(0);
    expect(field("Ticket ID (optional)")).toHaveAttribute("aria-invalid", "true");
    expect(field("Ticket ID (optional)")).toHaveFocus();
    expect(field("Category (required)")).toHaveAttribute("aria-invalid", "true");
    expect(field("Tags (optional)")).toHaveAccessibleDescription(expect.stringContaining('"not/ok"'));
  });
});

describe("saving a resolved ticket", () => {
  it("sends one request per click in the exact save shape, then shows the saved ticket once", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);
    enter(field("Tags (optional)"), " printer, smoke, ,printer ");
    await user.click(screen.getByRole("button", { name: "Unknown" }));
    expect(field("Root cause (optional)")).toHaveValue("Unknown");

    await user.dblClick(saveButton());
    expect(api.saveCalls).toHaveLength(1);
    expect(saveButton()).toHaveAttribute("aria-disabled", "true");
    expect(saveButton()).not.toBeDisabled();
    await user.click(saveButton());
    expect(api.saveCalls).toHaveLength(1);

    const body = api.saveCalls[0]?.body;
    expect(body).toEqual({
      submission_id: expect.stringMatching(UUID),
      ticket_id: null,
      subject: SUBJECT,
      description: DESCRIPTION,
      category: "Printing",
      product: null,
      error_detail: null,
      investigation: INVESTIGATION,
      root_cause: "Unknown",
      resolution_steps: STEPS,
      verification: VERIFICATION,
      resolved_by: "Agent Nine",
      tags: ["printer", "smoke"],
      confirm_resolved: true,
    });
    expect(body).not.toHaveProperty("expected_revision");

    await respond(api.saveCalls[0], 201, makeSaveResponse());

    expect(screen.getByText(SAVED_AND_SEARCHABLE)).toBeInTheDocument();
    expect(screen.getByText("T-9101", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Invented ticket T-9101 was created.")).toBeInTheDocument();
    expect(screen.getByText("Recorded as T-9101")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(SAVED_AND_SEARCHABLE);
    // The draft cannot be saved a second time: no Save button and no second form.
    expect(screen.queryByRole("button", { name: "Save resolved ticket" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record resolution" })).not.toBeInTheDocument();
    expect(api.saveCalls).toHaveLength(1);
  });

  it("keeps the form and reuses the submission id when the agent saves again after a network failure", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);

    await user.click(saveButton());
    await failNetwork(api.saveCalls[0]);

    expect(screen.getByText("The ticket service could not be reached")).toBeInTheDocument();
    expect(screen.queryByText(SAVED_AND_SEARCHABLE)).not.toBeInTheDocument();
    expect(field("Investigation approach (required)")).toHaveValue(INVESTIGATION);
    expect(saveButton()).not.toHaveAttribute("aria-disabled");

    // Closing and reopening the form keeps the same draft.
    await user.click(screen.getByRole("button", { name: "Close form" }));
    await openForm(user);
    expect(field("Investigation approach (required)")).toHaveValue(INVESTIGATION);

    await user.click(saveButton());
    expect(api.saveCalls).toHaveLength(2);
    expect(api.saveCalls[0]?.body.submission_id).toMatch(UUID);
    expect(api.saveCalls[1]?.body.submission_id).toBe(api.saveCalls[0]?.body.submission_id);
    expect(api.saveCalls[1]?.body).toEqual(api.saveCalls[0]?.body);

    await respond(
      api.saveCalls[1],
      200,
      makeSaveResponse({ outcome: "unchanged", repeat_submission: true }),
    );
    expect(screen.getByText(SAVED_AND_SEARCHABLE)).toBeInTheDocument();
  });

  it("says a ticket whose indexing failed is saved but not searchable, and retries indexing once per click", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);
    await user.click(saveButton());
    await respond(api.saveCalls[0], 201, makeSaveResponse({ indexing: makeFailedIndexing() }));

    expect(screen.getByText("Saved under T-9101, but not yet searchable")).toBeInTheDocument();
    expect(
      screen.getByText("The invented search index could not be updated right now."),
    ).toBeInTheDocument();
    expect(screen.getByText("It is not yet available to future searches.")).toBeInTheDocument();
    expect(screen.queryByText(SAVED_AND_SEARCHABLE)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent(SAVED_AND_SEARCHABLE);
    expect(screen.getByText("Recorded as T-9101")).toBeInTheDocument();
    expect(api.refreshCalls).toHaveLength(0);

    const retry = screen.getByRole("button", { name: "Retry indexing" });
    await user.dblClick(retry);
    expect(api.refreshCalls).toHaveLength(1);
    expect(api.refreshCalls[0]?.body).toBeNull();
    expect(retry).toHaveAttribute("aria-disabled", "true");

    await respond(
      api.refreshCalls[0],
      200,
      makeRefreshResponse({
        indexing: makeFailedIndexing({ message: "The invented index is still busy." }),
        unindexed_ticket_ids: ["T-9101"],
      }),
    );
    expect(screen.getByText("The invented index is still busy.")).toBeInTheDocument();
    expect(screen.queryByText(SAVED_AND_SEARCHABLE)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry indexing" }));
    expect(api.refreshCalls).toHaveLength(2);
    await respond(api.refreshCalls[1], 200, makeRefreshResponse());

    expect(screen.getByText(SAVED_AND_SEARCHABLE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry indexing" })).not.toBeInTheDocument();
    expect(api.saveCalls).toHaveLength(1);
    expect(api.refreshCalls).toHaveLength(2);
  });

  it("shows the stored version on a ticket conflict and replaces it only after the explicit button", async () => {
    const stored = makeStoredTicket({
      ticket_id: "T-9001",
      subject: "Stored invented subject",
      description: "Stored invented description",
      resolution_notes: "Stored invented resolution",
      revision: "rev-7",
    });
    const api = installFakeApi({ tickets: [stored] });
    const user = userEvent.setup();
    await readyToSave(api, user);
    enter(field("Ticket ID (optional)"), "T-9001");
    await user.click(saveButton());
    expect(api.saveCalls[0]?.body.ticket_id).toBe("T-9001");
    await respond(
      api.saveCalls[0],
      409,
      makeApiError("ticket_conflict", "Ticket T-9001 already exists with different content."),
    );

    expect(await screen.findByText("Stored invented subject")).toBeInTheDocument();
    expect(
      screen.getByText("Ticket T-9001 already exists with different content"),
    ).toBeInTheDocument();
    expect(screen.getByText("Stored invented description")).toBeInTheDocument();
    expect(screen.getByText("Stored invented resolution")).toBeInTheDocument();
    expect(screen.getByText("rev-7")).toBeInTheDocument();
    expect(api.ticketRequests).toEqual(["T-9001"]);
    // Nothing is replaced on its own, and the form is untouched.
    expect(api.saveCalls).toHaveLength(1);
    expect(screen.queryByText(SAVED_AND_SEARCHABLE)).not.toBeInTheDocument();
    expect(screen.queryByText(/Recorded as/)).not.toBeInTheDocument();
    expect(field("Ticket ID (optional)")).toHaveValue("T-9001");
    expect(field("Investigation approach (required)")).toHaveValue(INVESTIGATION);

    await user.click(screen.getByRole("button", { name: "Replace the stored version" }));
    expect(api.saveCalls).toHaveLength(2);
    expect(api.saveCalls[1]?.body).toEqual({ ...api.saveCalls[0]?.body, expected_revision: "rev-7" });

    await respond(
      api.saveCalls[1],
      200,
      makeSaveResponse({
        outcome: "updated",
        ticket: makeStoredTicket({ ticket_id: "T-9001", revision: "rev-8" }),
        message: "Invented ticket T-9001 was updated.",
      }),
    );
    expect(screen.getByText(SAVED_AND_SEARCHABLE)).toBeInTheDocument();
    expect(screen.getByText("Recorded as T-9001")).toBeInTheDocument();
  });

  it("can save as a new ticket instead after a conflict, without a ticket id", async () => {
    const stored = makeStoredTicket({ ticket_id: "T-9001", subject: "Stored invented subject" });
    const api = installFakeApi({ tickets: [stored] });
    const user = userEvent.setup();
    await readyToSave(api, user);
    enter(field("Ticket ID (optional)"), "T-9001");
    await user.click(saveButton());
    await respond(api.saveCalls[0], 409, makeApiError("ticket_conflict", "Conflict."));
    await screen.findByText("Stored invented subject");

    await user.click(screen.getByRole("button", { name: "Save as a new ticket instead" }));
    expect(api.saveCalls).toHaveLength(2);
    expect(api.saveCalls[1]?.body.ticket_id).toBeNull();
    expect(api.saveCalls[1]?.body).not.toHaveProperty("expected_revision");
    expect(api.saveCalls[1]?.body.submission_id).toBe(api.saveCalls[0]?.body.submission_id);
    expect(field("Ticket ID (optional)")).toHaveValue("");

    await respond(api.saveCalls[1], 201, makeSaveResponse());
    expect(screen.getByText("Recorded as T-9101")).toBeInTheDocument();
  });

  it.each([
    [503, "store_unavailable", "The ticket store is unavailable right now"],
    [503, "store_full", "The ticket store is full"],
    [403, "save_disabled", "Saving resolved tickets is switched off"],
    [409, "submission_conflict", "This form was already saved with different content"],
    [422, "validation_error", "The resolved ticket was not accepted as entered"],
    [500, "internal_error", "The ticket service had a problem"],
  ])("explains a %i %s and keeps the form so the agent can save again", async (status, code, title) => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);
    await user.click(saveButton());
    await respond(api.saveCalls[0], status, makeApiError(code, "Invented server message."));

    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.getByText("Invented server message.")).toBeInTheDocument();
    expect(screen.queryByText(SAVED_AND_SEARCHABLE)).not.toBeInTheDocument();
    expect(screen.queryByText(/Recorded as/)).not.toBeInTheDocument();
    expect(field("Investigation approach (required)")).toHaveValue(INVESTIGATION);
    expect(saveButton()).not.toHaveAttribute("aria-disabled");

    await user.click(saveButton());
    expect(api.saveCalls).toHaveLength(2);
    expect(api.saveCalls[1]?.body.submission_id).toBe(api.saveCalls[0]?.body.submission_id);
  });

  it("attaches 422 field details to their fields", async () => {
    const api = installFakeApi();
    const user = userEvent.setup();
    await readyToSave(api, user);
    await user.click(saveButton());
    await respond(
      api.saveCalls[0],
      422,
      makeApiError("validation_error", "Some fields were not accepted.", [
        { field: "category", message: "Invented rule: this category is not allowed." },
        { field: "body.confirm_resolved", message: "Invented rule: confirmation is required." },
      ]),
    );

    const category = field("Category (required)");
    expect(category).toHaveAttribute("aria-invalid", "true");
    expect(category).toHaveAccessibleDescription(
      expect.stringContaining("Invented rule: this category is not allowed."),
    );
    expect(category).toHaveFocus();
    expect(field("I confirm this issue has been resolved (required)")).toHaveAccessibleDescription(
      expect.stringContaining("Invented rule: confirmation is required."),
    );
    expect(field("Investigation approach (required)")).not.toHaveAttribute("aria-invalid");
  });

  it("fixes Resolved by to the signed-in identity", async () => {
    const api = installFakeApi({ meta: { ...metaFixture, identity: "agent.nine@example.org" } });
    const user = userEvent.setup();
    await searchFor(api, user, makeNoStrongMatch());
    await openForm(user);

    const resolvedBy = field("Resolved by (required)");
    expect(resolvedBy).toHaveValue("agent.nine@example.org");
    expect(resolvedBy).toHaveAttribute("readonly");
    expect(resolvedBy).toHaveAccessibleDescription(expect.stringContaining("signed-in identity"));

    await fillRequired(user, { resolvedBy: false });
    await user.click(saveButton());
    expect(api.saveCalls).toHaveLength(1);
    expect(api.saveCalls[0]?.body.resolved_by).toBe("agent.nine@example.org");
  });

  it("does not offer recording when the server has saving switched off", async () => {
    const api = installFakeApi({ meta: { ...metaFixture, save_enabled: false } });
    const user = userEvent.setup();
    await searchFor(api, user, makeNoStrongMatch());

    expect(screen.getByText("No relevant historical match found.")).toBeInTheDocument();
    expect(mailtoLinks()).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Record resolution" })).not.toBeInTheDocument();
    expect(screen.getByText(/Recording resolutions is switched off on this server/)).toBeInTheDocument();
  });
});
