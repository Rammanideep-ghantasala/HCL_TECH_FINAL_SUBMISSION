import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { REUSE_CAUTION, SCORES_NOTE } from "./constants";
import { makeCard, makeResponse, makeTicket, segmentsOf } from "./test/fixtures";
import { installFakeApi, renderApp, respond, searchButton, subjectInput } from "./test/harness";

const IMG_PAYLOAD = "<img src=x onerror=alert(1)>";
const SCRIPT_PAYLOAD = "<script>alert(1)</script>";

async function searchAndAnswer(
  api: ReturnType<typeof installFakeApi>,
  answer: ReturnType<typeof makeResponse>,
) {
  const user = userEvent.setup();
  await renderApp();
  await user.type(subjectInput(), "Alpha problem");
  await user.click(searchButton());
  await respond(api.searchCalls[0], 200, answer);
  return user;
}

function openDetails(summaryText: string, scope: HTMLElement) {
  const details = within(scope).getByText(summaryText).closest("details");
  if (!details) {
    throw new Error(`No disclosure named ${summaryText}`);
  }
  details.open = true;
  fireEvent(details, new Event("toggle"));
  return details;
}

describe("result card", () => {
  it("displays untrusted ticket text literally and creates no elements from it", async () => {
    const hostile = makeTicket({
      ticket_id: "T-9003",
      subject: `Broken ${IMG_PAYLOAD} subject`,
      description: `Description with ${SCRIPT_PAYLOAD} inside`,
      resolution_notes: `Resolution ${IMG_PAYLOAD} and ${SCRIPT_PAYLOAD} kept as text`,
      tags: [IMG_PAYLOAD],
    });
    const linked = makeTicket({
      ticket_id: "T-9004",
      subject: `Linked ${SCRIPT_PAYLOAD} subject`,
      description: `Linked description ${IMG_PAYLOAD}`,
      resolution_notes: `Linked resolution ${IMG_PAYLOAD}`,
    });
    const api = installFakeApi({ tickets: [hostile, linked] });
    const card = makeCard({
      ticket: hostile,
      sharedTerm: IMG_PAYLOAD,
      newSubject: `New ${IMG_PAYLOAD} ticket`,
      card: {
        referenced_ticket_ids: ["T-9004"],
        evidence: {
          found_by: ["keyword"],
          keyword_rank: 1,
          semantic_rank: null,
          shared_terms: [IMG_PAYLOAD, SCRIPT_PAYLOAD],
          new_subject_segments: segmentsOf(`New ${IMG_PAYLOAD} ticket`, IMG_PAYLOAD),
          new_description_segments: segmentsOf(SCRIPT_PAYLOAD, SCRIPT_PAYLOAD),
          past_subject_segments: segmentsOf(hostile.subject, IMG_PAYLOAD),
          past_description_segments: segmentsOf(hostile.description, SCRIPT_PAYLOAD),
          excerpts: [{ field: "description", segments: segmentsOf(hostile.description, SCRIPT_PAYLOAD) }],
          retrieval_note: `Note mentioning ${SCRIPT_PAYLOAD}`,
        },
      },
    });

    const user = await searchAndAnswer(
      api,
      makeResponse({ message: `Message with ${IMG_PAYLOAD}`, results: [card] }),
    );
    const article = screen.getByRole("article");

    await user.click(within(article).getByRole("button", { name: "View T-9004" }));
    await within(article).findByText(linked.resolution_notes);
    openDetails("Full original record", article);
    await within(article).findByText("Resolution notes");

    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("[onerror]")).toBeNull();

    expect(within(article).getAllByText(hostile.resolution_notes).length).toBeGreaterThan(0);
    expect(screen.getByText(`Message with ${IMG_PAYLOAD}`)).toBeInTheDocument();
    expect(within(article).getByText(linked.subject)).toBeInTheDocument();

    const marks = Array.from(article.querySelectorAll("mark")).map((mark) => mark.textContent);
    expect(marks).toContain(IMG_PAYLOAD);
    expect(marks).toContain(SCRIPT_PAYLOAD);
  });

  it("shows the resolution verbatim, the evidence, unknowns as unknown, and the fixed cautions", async () => {
    const api = installFakeApi({ tickets: [makeTicket()] });
    const ticket = makeTicket({ category: null, status: null, tags: [], created_at: null });
    await searchAndAnswer(api, makeResponse({ results: [makeCard({ ticket })] }));
    const article = screen.getByRole("article");

    expect(
      within(article).getByRole("heading", { name: "Original resolution (verbatim from T-9001)" }),
    ).toBeInTheDocument();
    expect(within(article).getByText(ticket.resolution_notes)).toBeInTheDocument();
    expect(within(article).getByText("Strong textual match")).toBeInTheDocument();
    expect(within(article).getAllByText("not recorded").length).toBeGreaterThanOrEqual(3);
    expect(
      within(article).getByText("(review status of the fix is not recorded in this dataset)"),
    ).toBeInTheDocument();

    const table = within(article).getByRole("table");
    const productRow = within(table).getByRole("row", { name: /Product or system/ });
    expect(within(productRow).getAllByText("Unknown").length).toBeGreaterThanOrEqual(2);
    expect(within(productRow).queryByText("Different")).not.toBeInTheDocument();
    const codeRow = within(table).getByRole("row", { name: /Error code/ });
    expect(within(codeRow).getByText("Different")).toBeInTheDocument();

    expect(within(article).getByText(/^Words in the new ticket that .+ does not use$/)).toBeInTheDocument();
    expect(within(article).getByText(/^Words in .+ that the new ticket does not use$/)).toBeInTheDocument();
    expect(within(article).getByText(/absent means not stated, not different/)).toBeInTheDocument();
    expect(within(article).queryByText("Not found in any past ticket")).not.toBeInTheDocument();

    expect(
      within(article).getByText("Does the customer see code WX-17 as the past ticket did?"),
    ).toBeInTheDocument();
    expect(within(article).getByText(REUSE_CAUTION)).toBeInTheDocument();
    expect(within(article).getByText(SCORES_NOTE)).toBeInTheDocument();
    expect(within(article).queryByText(/%/)).not.toBeInTheDocument();
  });

  it("shows recorded investigation fields only when present, in the card and the full record", async () => {
    const recorded = makeTicket({
      ticket_id: "T-9006",
      resolution_notes: "Cleared the invented stuck job.",
      product: "Invented Spooler 3",
      error_detail: "SPL-42",
      investigation: "Checked the invented spooler log.",
      root_cause: "Stale invented token",
      verification: "Printed an invented test page.",
      resolved_by: "Agent Nine",
      resolved_at: "2031-03-04T05:06:07Z",
    });
    const plain = makeTicket({ ticket_id: "T-9007" });
    const api = installFakeApi({ tickets: [recorded] });
    await searchAndAnswer(
      api,
      makeResponse({
        results: [
          makeCard({ ticket: recorded }),
          makeCard({ ticket: plain, card: { result_id: "res-T-9007", rank: 2 } }),
        ],
      }),
    );
    const [first, second] = screen.getAllByRole("article");
    if (!first || !second) {
      throw new Error("Two result cards were expected.");
    }

    expect(
      within(first).getByRole("heading", { name: "Recorded investigation (from T-9006)" }),
    ).toBeInTheDocument();
    expect(
      within(first).getByRole("heading", { name: "Original resolution (verbatim from T-9006)" }),
    ).toBeInTheDocument();
    expect(within(first).getByText("Cleared the invented stuck job.")).toBeInTheDocument();
    expect(within(first).getByText("Checked the invented spooler log.")).toBeInTheDocument();
    expect(within(first).getByText("Stale invented token")).toBeInTheDocument();
    expect(within(first).getByText("Printed an invented test page.")).toBeInTheDocument();
    expect(within(first).getByText("Agent Nine")).toBeInTheDocument();
    expect(within(first).getByText("2031-03-04T05:06:07Z")).toBeInTheDocument();
    expect(within(second).queryByText(/Recorded investigation/)).not.toBeInTheDocument();
    expect(within(second).queryByText("Investigation approach")).not.toBeInTheDocument();

    openDetails("Full original record", first);
    await within(first).findByText("Ticket id");
    expect(within(first).getByText("Invented Spooler 3")).toBeInTheDocument();
    expect(within(first).getByText("SPL-42")).toBeInTheDocument();
    expect(within(first).getAllByText("Checked the invented spooler log.")).toHaveLength(2);
    expect(within(first).getAllByText("Resolved at (UTC)")).toHaveLength(2);
  });

  it("fetches a stored ticket once, on first use, and reports a missing ticket", async () => {
    const api = installFakeApi({ tickets: [makeTicket()] });
    const card = makeCard({ card: { referenced_ticket_ids: ["T-9777"] } });
    const user = await searchAndAnswer(api, makeResponse({ results: [card] }));
    const article = screen.getByRole("article");
    expect(api.ticketRequests).toHaveLength(0);

    const details = openDetails("Full original record", article);
    await within(details).findByText("Ticket id");
    details.open = false;
    fireEvent(details, new Event("toggle"));
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(api.ticketRequests).toEqual(["T-9001"]);

    await user.click(within(article).getByRole("button", { name: "View T-9777" }));
    expect(
      await within(article).findByText("Ticket T-9777 was not found in the stored history."),
    ).toBeInTheDocument();
    expect(api.ticketRequests).toEqual(["T-9001", "T-9777"]);
  });
});
