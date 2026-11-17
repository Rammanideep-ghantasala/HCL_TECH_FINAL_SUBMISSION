import { Fragment, useEffect, useId, useState, type SyntheticEvent } from "react";
import type { FetchedTicket } from "../api/client";
import type { Ticket } from "../api/types";
import { NOT_RECORDED } from "../constants";
import { useTicket, type TicketEntry } from "../hooks/ticketCache";
import { recordedInvestigation, recordedProblemDetails } from "../state/resolution";

export const STORED_NOT_IN_SEARCH = "Stored version, not yet in search results";
export const STORED_REJECTED_NOTE =
  "It cannot be indexed as written (it failed validation), so it stays out of search results until its content changes.";
export const STORED_DIFFERS_FROM_CARD =
  "The stored version differs from the version shown on this card, which is the one the search used.";

function orNotRecorded(value: string | null): string {
  return value === null || value.trim() === "" ? NOT_RECORDED : value;
}

/** Fields compared to tell whether the stored ticket is the version a result card shows. */
const COMPARED_FIELDS = [
  "subject",
  "description",
  "resolution_notes",
  "category",
  "status",
  "created_at",
  "product",
  "error_detail",
  "investigation",
  "root_cause",
  "verification",
  "resolved_by",
  "resolved_at",
] as const satisfies ReadonlyArray<keyof Ticket>;

/** True when the stored ticket's recorded content is not what the card showed. */
export function storedDiffersFrom(stored: Ticket, shown: Ticket): boolean {
  if (COMPARED_FIELDS.some((field) => (stored[field] ?? null) !== (shown[field] ?? null))) {
    return true;
  }
  return (
    stored.tags.length !== shown.tags.length ||
    stored.tags.some((tag, index) => tag !== shown.tags[index])
  );
}

/** Says when the fetched version is not the one in search results or not the one on the card. */
function VersionNotes({ ticket, shown }: { ticket: FetchedTicket; shown: Ticket | undefined }) {
  const notIndexed = ticket.indexed === false;
  const differs = shown !== undefined && storedDiffersFrom(ticket, shown);
  if (!notIndexed && !differs) {
    return null;
  }
  return (
    <div className="inline-warning version-notes">
      {notIndexed ? (
        <p>
          <strong>{STORED_NOT_IN_SEARCH}</strong>
        </p>
      ) : null}
      {notIndexed && ticket.index_status === "rejected" ? <p>{STORED_REJECTED_NOTE}</p> : null}
      {differs ? <p>{STORED_DIFFERS_FROM_CARD}</p> : null}
    </div>
  );
}

/** Subject, description and resolution of a stored ticket. */
function TicketSummary({ ticket }: { ticket: Ticket }) {
  return (
    <dl className="record">
      <dt>Subject</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.subject)}</dd>
      <dt>Description</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.description)}</dd>
      <dt>Original resolution (verbatim from {ticket.ticket_id})</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.resolution_notes)}</dd>
    </dl>
  );
}

/** Every stored field of a ticket, exactly as recorded. Fields added by the resolution form appear only when present. */
function TicketRecord({ ticket }: { ticket: Ticket }) {
  const recorded = [...recordedProblemDetails(ticket), ...recordedInvestigation(ticket)];
  return (
    <dl className="record">
      <dt>Ticket id</dt>
      <dd className="ticket-text">{ticket.ticket_id}</dd>
      <dt>Category</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.category)}</dd>
      <dt>Status</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.status)}</dd>
      <dt>Created</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.created_at)}</dd>
      <dt>Tags</dt>
      <dd className="ticket-text">{ticket.tags.length === 0 ? NOT_RECORDED : ticket.tags.join(", ")}</dd>
      <dt>Subject</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.subject)}</dd>
      <dt>Description</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.description)}</dd>
      <dt>Resolution notes</dt>
      <dd className="ticket-text">{orNotRecorded(ticket.resolution_notes)}</dd>
      {recorded.map((item) => (
        <Fragment key={item.key}>
          <dt>{item.label}</dt>
          <dd className="ticket-text">{item.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

interface LookupBodyProps {
  ticketId: string;
  entry: TicketEntry | undefined;
  onRetry: () => void;
  variant: "summary" | "record";
  /** The version a result card shows, when the lookup belongs to that card. */
  shown?: Ticket;
}

function LookupBody({ ticketId, entry, onRetry, variant, shown }: LookupBodyProps) {
  if (entry === undefined || entry.status === "loading") {
    return <p className="muted">Loading {ticketId}…</p>;
  }
  if (entry.status === "error") {
    return (
      <div className="lookup-error">
        <p>{entry.message}</p>
        <button type="button" className="button button-small" onClick={onRetry}>
          Try loading {ticketId} again
        </button>
      </div>
    );
  }
  return (
    <>
      <VersionNotes ticket={entry.ticket} shown={shown} />
      {variant === "summary" ? (
        <TicketSummary ticket={entry.ticket} />
      ) : (
        <TicketRecord ticket={entry.ticket} />
      )}
    </>
  );
}

/**
 * Fetches the ticket while its view is open and nothing is cached for it:
 * on first opening, and again after the cache forgot it (for example after
 * this page saved that ticket). A failed lookup waits for "Try again".
 */
function useLoadWhileOpen(open: boolean, entry: TicketEntry | undefined, load: () => void) {
  useEffect(() => {
    if (open && entry === undefined) {
      load();
    }
  }, [open, entry, load]);
}

/** A "View <id>" button that shows another stored ticket inline. */
export function ReferencedTicket({ ticketId }: { ticketId: string }) {
  const [open, setOpen] = useState(false);
  const { entry, load } = useTicket(ticketId);
  const panelId = useId();
  useLoadWhileOpen(open, entry, load);

  return (
    <li className="referenced">
      <button
        type="button"
        className="button button-small"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(!open)}
      >
        {open ? `Hide ${ticketId}` : `View ${ticketId}`}
      </button>
      <div id={panelId} className="referenced-panel" hidden={!open}>
        {open ? (
          <LookupBody ticketId={ticketId} entry={entry} onRetry={load} variant="summary" />
        ) : null}
      </div>
    </li>
  );
}

/**
 * A native disclosure that fetches the complete stored record the first time
 * it opens. `shown` is the version on the result card, so that a stored
 * version that differs from it can be pointed out.
 */
export function FullRecord({ ticketId, shown }: { ticketId: string; shown?: Ticket }) {
  const [open, setOpen] = useState(false);
  const { entry, load } = useTicket(ticketId);
  useLoadWhileOpen(open, entry, load);

  const handleToggle = (event: SyntheticEvent<HTMLDetailsElement>) => {
    setOpen(event.currentTarget.open);
  };

  return (
    <details className="disclosure" onToggle={handleToggle}>
      <summary>Full original record</summary>
      <div className="disclosure-body">
        <LookupBody ticketId={ticketId} entry={entry} onRetry={load} variant="record" shown={shown} />
      </div>
    </details>
  );
}
