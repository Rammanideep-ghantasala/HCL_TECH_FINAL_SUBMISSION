import type { ReactNode, RefObject } from "react";
import type {
  ExactMatchInfo,
  ResultCard as ResultCardData,
  RetrievalInfo,
  SearchResponse,
} from "../api/types";
import { EXACT_MATCH_CAUTION, EXACT_MATCH_EXPLANATION } from "../constants";
import { escalationMailto } from "../state/resolution";
import {
  FIELD_LABELS,
  summariseInput,
  type FailureKind,
  type FieldName,
  type FormValues,
  type SearchState,
} from "../state/search";
import type { FeedbackRecord } from "../storage/session";
import { Banner } from "./Banner";
import { ErrorBoundary } from "./ErrorBoundary";
import { ResultCard, type ResultCardProps } from "./ResultCard";
import { SuggestionList } from "./SuggestionList";
import { ReferencedTicket } from "./TicketLookup";

const HEADINGS: Record<SearchState["phase"], string> = {
  idle: "Before you search",
  loading: "Searching",
  done: "Search results",
  failed: "Search did not complete",
};

const SUBMITTED_PREFIX: Record<Exclude<SearchState["phase"], "idle">, string> = {
  loading: "Searching for:",
  done: "Results for:",
  failed: "Searched for:",
};

const FAILURE_TITLES: Record<FailureKind, string> = {
  timeout: "The search timed out",
  network: "The search service could not be reached",
  server: "The search service had a problem",
  rejected: "The ticket was not accepted as entered",
};

const FAILURE_ADVICE: Record<FailureKind, string> = {
  timeout: "Nothing was returned. You can retry; the same ticket is sent again once.",
  network: "Nothing was sent or nothing came back. Check your connection, then retry.",
  server: "Nothing was returned. Wait a moment, then retry.",
  rejected: "Correct the field marked in the form and press Search again.",
};

function IdleExplanation() {
  return (
    <div className="idle">
      <p>
        Enter a new support ticket. The assistant looks for similar past resolved tickets and shows
        each one's original resolution exactly as it was written, with the evidence for the match
        and the differences you should check.
      </p>
      <div className="idle-columns">
        <div>
          <h3 className="section-heading">What it does</h3>
          <ul>
            <li>Finds past tickets with similar wording or meaning.</li>
            <li>Shows why each ticket was retrieved and what differs.</li>
            <li>Says plainly when there is no strong match.</li>
          </ul>
        </div>
        <div>
          <h3 className="section-heading">What it does not do</h3>
          <ul>
            <li>It does not write or recommend a resolution of its own.</li>
            <li>It does not issue refunds, reset anything, change accounts or close tickets.</li>
            <li>It does not know whether a past fix will work for this customer. You decide.</li>
          </ul>
        </div>
      </div>
    </div>
  );
}

function SubmittedSummary({
  phase,
  submitted,
  formEdited,
}: {
  phase: Exclude<SearchState["phase"], "idle">;
  submitted: FormValues;
  formEdited: boolean;
}) {
  const extras: Array<{ label: string; value: string }> = [];
  if (submitted.product.trim() !== "") {
    extras.push({ label: FIELD_LABELS.product, value: submitted.product.trim() });
  }
  if (submitted.error_detail.trim() !== "") {
    extras.push({ label: FIELD_LABELS.error_detail, value: submitted.error_detail.trim() });
  }
  return (
    <div className="submitted">
      <p className="submitted-line">
        {SUBMITTED_PREFIX[phase]} <strong className="ticket-text">{summariseInput(submitted)}</strong>
      </p>
      {extras.length > 0 ? (
        <ul className="submitted-extras">
          {extras.map((extra) => (
            <li key={extra.label}>
              {extra.label}: <span className="ticket-text">{extra.value}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {formEdited ? (
        <p className="edited-note">The form has been edited since this search.</p>
      ) : null}
    </div>
  );
}

function degradedTitle(retrieval: RetrievalInfo): string {
  if (retrieval.mode === "keyword_only") {
    return "Keyword-only results: meaning-based search is unavailable right now";
  }
  if (retrieval.mode === "semantic_only") {
    return "Meaning-only results: keyword search is unavailable right now";
  }
  return "Reduced search: part of the search was unavailable for this request";
}

function DegradedBanner({ retrieval }: { retrieval: RetrievalInfo }) {
  return (
    <Banner tone="warning" tag="Reduced search" title={degradedTitle(retrieval)}>
      <p>Some similar tickets may be missing or ranked differently from a full search.</p>
      {retrieval.degraded_reason ? (
        <p className="basis">Reported reason: {retrieval.degraded_reason}</p>
      ) : null}
    </Banner>
  );
}

function FilterNotice({
  retrieval,
  onClearFilterAndSearch,
}: {
  retrieval: RetrievalInfo;
  onClearFilterAndSearch: () => void;
}) {
  const hidden = retrieval.hidden_by_filter;
  return (
    <Banner
      tone="neutral"
      tag="Filter"
      title={`Category filter active: ${retrieval.category_filter ?? ""}`}
    >
      <p>Only past tickets filed under this category are considered.</p>
      {hidden > 0 ? (
        <p>
          <strong>
            {hidden === 1
              ? "1 potentially useful ticket in another category is hidden by this filter."
              : `${hidden} potentially useful tickets in other categories are hidden by this filter.`}
          </strong>
        </p>
      ) : null}
      <button type="button" className="button button-small" onClick={onClearFilterAndSearch}>
        Clear filter and search again
      </button>
    </Banner>
  );
}

/** What the server found about a ticket id the caller supplied. Never shown when no id was sent. */
function TicketIdNotice({ exact }: { exact: ExactMatchInfo }) {
  if (exact.message === null || exact.ticket_id === null) {
    return null;
  }
  const conflict = exact.ticket_id_status === "exists_different_text";
  return (
    <Banner
      tone={conflict ? "warning" : "neutral"}
      tag="Ticket ID"
      title={conflict ? `${exact.ticket_id} already exists with different text` : `Ticket ID ${exact.ticket_id}`}
    >
      <p>{exact.message}</p>
      {conflict ? (
        <ul className="referenced-list">
          <ReferencedTicket ticketId={exact.ticket_id} />
        </ul>
      ) : null}
    </Banner>
  );
}

function ExactMatchBanner({ cards }: { cards: ResultCardData[] }) {
  const ids = cards.map((card) => card.ticket.ticket_id);
  return (
    <Banner tone="info" tag="Exact text match" title="Exact text match found">
      <p>{EXACT_MATCH_EXPLANATION}</p>
      <p>
        Past {ids.length === 1 ? "ticket" : "tickets"} with identical wording:{" "}
        <strong>{ids.join(", ")}</strong>
      </p>
      <p>{EXACT_MATCH_CAUTION}</p>
      <p className="basis">No similarity search was run for this ticket.</p>
    </Banner>
  );
}

interface CardListProps {
  cards: ResultCardData[];
  feedback: FeedbackRecord[];
  onFeedback: ResultCardProps["onFeedback"];
}

function CardList({ cards, feedback, onFeedback }: CardListProps) {
  return (
    <div className="card-list">
      {cards.map((card) => (
        <ResultCard
          key={card.result_id}
          card={card}
          feedback={feedback.find((record) => record.result_id === card.result_id)}
          onFeedback={onFeedback}
        />
      ))}
    </div>
  );
}

function BelowThreshold({ title, ...list }: CardListProps & { title: string }) {
  if (list.cards.length === 0) {
    return null;
  }
  return (
    <details className="disclosure below-threshold">
      <summary>
        {title} <span className="count">({list.cards.length})</span>
      </summary>
      <div className="disclosure-body">
        <p className="basis">
          These tickets did not pass the match threshold. Treat them as background, not as likely
          answers.
        </p>
        <CardList {...list} />
      </div>
    </details>
  );
}

/** What the no-match outcome needs to offer escalation and recording a resolution. */
export interface NoMatchActions {
  escalationEmail: string;
  /** False when the server reports that saving resolved tickets is switched off. */
  saveEnabled: boolean;
  /** The id this search's ticket was saved under, once it was saved. */
  savedTicketId: string | null;
  formOpen: boolean;
  recordButtonRef: RefObject<HTMLButtonElement | null>;
  onRecordResolution: () => void;
  /** The resolution form for this search, when it is open. */
  form: ReactNode;
}

/** The escalation sentence. The address is the only link; its subject is fixed text. */
function HumanInvestigationRequired({ escalationEmail }: { escalationEmail: string }) {
  return (
    <>
      <p>
        <strong>Human investigation is required.</strong> Contact{" "}
        <a className="mail-link" href={escalationMailto(escalationEmail)}>
          {escalationEmail}
        </a>
        .
      </p>
      <p className="basis">
        The link opens a new email in your mail program. Nothing is sent automatically, and no
        ticket text is added to it; include the details you choose to share.
      </p>
    </>
  );
}

function NoMatchNextSteps({
  saveEnabled,
  savedTicketId,
  formOpen,
  recordButtonRef,
  onRecordResolution,
}: NoMatchActions) {
  let recordArea: ReactNode;
  if (savedTicketId !== null) {
    recordArea = (
      <p className="recorded">
        <strong>Recorded as {savedTicketId}</strong>
      </p>
    );
  } else if (saveEnabled) {
    recordArea = (
      <button
        type="button"
        ref={recordButtonRef}
        className="button button-primary"
        aria-expanded={formOpen}
        aria-controls={formOpen ? "resolution-form" : undefined}
        onClick={onRecordResolution}
      >
        Record resolution
      </button>
    );
  } else {
    recordArea = (
      <p className="inline-warning">
        <span className="inline-warning-tag">Note:</span> Recording resolutions is switched off on
        this server.
      </p>
    );
  }

  return (
    <section className="next-steps" aria-labelledby="next-steps-heading">
      <h3 id="next-steps-heading" className="section-heading">
        Next steps
      </h3>
      <p>
        After you solve the issue, record your investigation approach and the resolution so this
        ticket can help future searches.
      </p>
      {recordArea}
    </section>
  );
}

interface DoneProps {
  response: SearchResponse;
  feedback: FeedbackRecord[];
  onFeedback: ResultCardProps["onFeedback"];
  onGoToField: (field: FieldName) => void;
  onClearFilterAndSearch: () => void;
  noMatch: NoMatchActions;
}

function DoneOutcome({
  response,
  feedback,
  onFeedback,
  onGoToField,
  onClearFilterAndSearch,
  noMatch,
}: DoneProps) {
  const { retrieval, query_insights: insights } = response;
  const exactCards = response.results.filter((card) => card.match_level === "exact");
  const notices = (
    <>
      {response.exact_match ? <TicketIdNotice exact={response.exact_match} /> : null}
      {retrieval.degraded ? <DegradedBanner retrieval={retrieval} /> : null}
      {retrieval.category_filter ? (
        <FilterNotice retrieval={retrieval} onClearFilterAndSearch={onClearFilterAndSearch} />
      ) : null}
    </>
  );

  if (response.status === "insufficient_input") {
    return (
      <>
        {notices}
        <Banner tone="neutral" tag="Not searched" title="More detail is needed before searching">
          <p>{response.message}</p>
          <p className="basis">No search was run, so no past tickets are shown.</p>
        </Banner>
        <SuggestionList
          heading="What to add"
          suggestions={insights.suggestions}
          onGoToField={onGoToField}
        />
      </>
    );
  }

  if (response.status === "no_strong_match") {
    // No previous-ticket cards here, not even below-threshold ones: none is a relevant match.
    // The server's own message is not shown either: it can refer to "the tickets below",
    // which are hidden here. The fixed sentences say everything this outcome needs.
    return (
      <>
        {notices}
        <Banner tone="info" tag="Search result" title="No relevant historical match found.">
          <p>No previous matching tickets are available for this search.</p>
          <HumanInvestigationRequired escalationEmail={noMatch.escalationEmail} />
          <p className="basis">The search ran normally. This is a search result, not an error.</p>
          {insights.unseen_terms.length > 0 ? (
            <p className="ticket-text">
              Not found in any of the {retrieval.corpus_size} past tickets:{" "}
              {insights.unseen_terms.join(", ")}
            </p>
          ) : null}
        </Banner>
        <NoMatchNextSteps {...noMatch} />
        {noMatch.form}
        <SuggestionList
          heading="Ways to refine this search"
          suggestions={insights.suggestions}
          onGoToField={onGoToField}
        />
      </>
    );
  }

  return (
    <>
      {notices}
      {exactCards.length > 0 ? <ExactMatchBanner cards={exactCards} /> : null}
      <p className="outcome-message">{response.message}</p>
      <CardList cards={response.results} feedback={feedback} onFeedback={onFeedback} />
      <BelowThreshold
        title="Related context (below the match threshold)"
        cards={response.related}
        feedback={feedback}
        onFeedback={onFeedback}
      />
      <SuggestionList
        heading="Ways to refine this search"
        suggestions={insights.suggestions}
        onGoToField={onGoToField}
      />
    </>
  );
}

function FailedOutcome({
  state,
  onRetry,
}: {
  state: Extract<SearchState, { phase: "failed" }>;
  onRetry: () => void;
}) {
  const isRejected = state.kind === "rejected";
  return (
    <Banner
      tone="error"
      tag={isRejected ? "Not accepted" : "Service problem"}
      title={FAILURE_TITLES[state.kind]}
    >
      <p>{state.message}</p>
      <p>{FAILURE_ADVICE[state.kind]}</p>
      {isRejected ? null : (
        <p>
          This is a problem with the service, not a search result. It says nothing about whether
          similar past tickets exist.
        </p>
      )}
      <p className="basis">Your ticket is still in the form. Nothing was changed.</p>
      {state.requestId ? (
        <p className="basis">
          Request id (quote this when reporting the problem):{" "}
          <code className="request-id">{state.requestId}</code>
        </p>
      ) : null}
      {isRejected ? null : (
        <button type="button" className="button button-primary" onClick={onRetry}>
          Retry
        </button>
      )}
    </Banner>
  );
}

export interface OutcomeAreaProps {
  state: SearchState;
  /** True when the live form no longer matches what was searched. */
  formEdited: boolean;
  headingRef: RefObject<HTMLHeadingElement | null>;
  feedback: FeedbackRecord[];
  onRetry: () => void;
  onClearFilterAndSearch: () => void;
  onGoToField: (field: FieldName) => void;
  onFeedback: ResultCardProps["onFeedback"];
  /** Escalation and resolution recording, shown only for a completed no-match search. */
  noMatch: NoMatchActions;
  summary?: ReactNode;
}

/** Everything below the form: explanation, progress, results or a failure. */
export function OutcomeArea({
  state,
  formEdited,
  headingRef,
  feedback,
  onRetry,
  onClearFilterAndSearch,
  onGoToField,
  onFeedback,
  noMatch,
  summary,
}: OutcomeAreaProps) {
  return (
    <section className="panel outcome" aria-labelledby="outcome-heading">
      <h2 id="outcome-heading" ref={headingRef} tabIndex={-1} className="panel-heading">
        {HEADINGS[state.phase]}
      </h2>

      {state.phase === "idle" ? (
        <IdleExplanation />
      ) : (
        <SubmittedSummary phase={state.phase} submitted={state.submitted} formEdited={formEdited} />
      )}

      {state.phase === "loading" ? (
        <p className="loading">
          <span className="loading-dot" aria-hidden="true" />
          Searching past tickets…
        </p>
      ) : null}

      {state.phase === "done" ? (
        <ErrorBoundary key={state.response.request_id}>
          {summary}
          <DoneOutcome
            response={state.response}
            feedback={feedback}
            onFeedback={onFeedback}
            onGoToField={onGoToField}
            onClearFilterAndSearch={onClearFilterAndSearch}
            noMatch={noMatch}
          />
        </ErrorBoundary>
      ) : null}

      {state.phase === "failed" ? <FailedOutcome state={state} onRetry={onRetry} /> : null}
    </section>
  );
}
