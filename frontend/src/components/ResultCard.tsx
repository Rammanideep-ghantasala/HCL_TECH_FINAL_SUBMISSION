import { Fragment, useId } from "react";
import type { ResultCard as ResultCardData, Scores } from "../api/types";
import { NOT_RECORDED, REUSE_CAUTION, SCORES_NOTE, STATUS_NOTE } from "../constants";
import { recordedInvestigation } from "../state/resolution";
import type { FeedbackReason, FeedbackRecord, Verdict } from "../storage/session";
import { ComparisonTable } from "./ComparisonTable";
import { FeedbackControls } from "./FeedbackControls";
import { SegmentedText } from "./SegmentedText";
import { TermList } from "./TermList";
import { FullRecord, ReferencedTicket } from "./TicketLookup";

const EXCERPT_FIELD_LABELS = { subject: "Subject", description: "Description" } as const;

function orNotRecorded(value: string | null): string {
  return value === null || value.trim() === "" ? NOT_RECORDED : value;
}

const FOUND_BY_LABELS: Record<string, string> = {
  keyword: "keyword search",
  semantic: "meaning-based search",
  exact_text: "exact text comparison",
};

function formatRank(rank: number | null): string {
  return rank === null ? "not ranked by this method" : String(rank);
}

function formatScore(score: number | null): string {
  return score === null ? "not available" : String(Number(score.toFixed(4)));
}

function ScoreRows({ scores }: { scores: Scores }) {
  return (
    <>
      <dt>Keyword cosine</dt>
      <dd>{formatScore(scores.keyword_cosine)}</dd>
      <dt>Semantic cosine</dt>
      <dd>{formatScore(scores.semantic_cosine)}</dd>
      <dt>Coverage of the new ticket&apos;s wording</dt>
      <dd>{formatScore(scores.query_coverage)}</dd>
      <dt>Reciprocal rank fusion</dt>
      <dd>{formatScore(scores.rrf)}</dd>
    </>
  );
}

export interface ResultCardProps {
  card: ResultCardData;
  /** The verdict already recorded for this result in this tab, if any. */
  feedback: FeedbackRecord | undefined;
  onFeedback: (
    card: ResultCardData,
    verdict: Verdict | null,
    reason: FeedbackReason | null,
  ) => void;
}

/**
 * One past ticket with everything needed to judge whether its resolution
 * applies. Used for useful matches and for related context alike.
 */
export function ResultCard({ card, feedback, onFeedback }: ResultCardProps) {
  const headingId = useId();
  const { ticket, evidence, differences } = card;
  const id = ticket.ticket_id;
  const referencedIds = [...new Set(card.referenced_ticket_ids)];
  const recorded = recordedInvestigation(ticket);

  return (
    <article id={`ticket-result-${id}`} className={`card card-${card.match_level}`} aria-labelledby={headingId}>
      <header className="card-header">
        <h3 id={headingId} className="card-title">
          <span className="card-rank">Result {card.rank}</span>
          <span className="card-id">{id}</span>
          <span className="card-subject ticket-text">{orNotRecorded(ticket.subject)}</span>
        </h3>
        <span className={`match-chip match-chip-${card.match_level}`}>{card.match_label}</span>
      </header>

      <dl className="meta">
        <div className="meta-item">
          <dt>Category</dt>
          <dd>{orNotRecorded(ticket.category)}</dd>
        </div>
        <div className="meta-item">
          <dt>Status</dt>
          <dd>
            {orNotRecorded(ticket.status)} <span className="basis">({STATUS_NOTE})</span>
          </dd>
        </div>
        <div className="meta-item">
          <dt>Tags</dt>
          <dd>{ticket.tags.length === 0 ? NOT_RECORDED : ticket.tags.join(", ")}</dd>
        </div>
        <div className="meta-item">
          <dt>Created</dt>
          <dd>{orNotRecorded(ticket.created_at)}</dd>
        </div>
      </dl>

      <section className="card-section resolution">
        <h4 className="card-section-heading">Original resolution (verbatim from {id})</h4>
        {ticket.resolution_notes.trim() === "" ? (
          <p className="muted">No resolution text is recorded for {id}.</p>
        ) : (
          <p className="verbatim ticket-text">{ticket.resolution_notes}</p>
        )}
      </section>

      {recorded.length > 0 ? (
        <section className="card-section">
          <h4 className="card-section-heading">Recorded investigation (from {id})</h4>
          <dl className="record">
            {recorded.map((item) => (
              <Fragment key={item.key}>
                <dt>{item.label}</dt>
                <dd className="ticket-text">{item.value}</dd>
              </Fragment>
            ))}
          </dl>
        </section>
      ) : null}

      <section className="card-section">
        <h4 className="card-section-heading">Why this ticket was retrieved</h4>
        <TermList
          terms={evidence.shared_terms}
          emptyText="No shared wording"
          label="Wording shared with the new ticket"
        />
        <p className="retrieval-note">{evidence.retrieval_note}</p>
        {evidence.excerpts.length > 0 ? (
          <div className="excerpts">
            <p className="list-label">Sentences in {id} with the most shared wording</p>
            <ul className="excerpt-list">
              {evidence.excerpts.map((excerpt, index) => (
                <li key={index}>
                  <span className="basis">{EXCERPT_FIELD_LABELS[excerpt.field] ?? "Text"}: </span>
                  <SegmentedText segments={excerpt.segments} emptyText="(empty)" />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className="card-section">
        <h4 className="card-section-heading">Side by side</h4>
        <p className="basis">
          Highlighted and underlined text marks wording that appears in both tickets.
        </p>
        <div className="side-by-side">
          <div className="side">
            <h5 className="side-heading">New ticket (as searched)</h5>
            <p className="side-label">Subject</p>
            <p className="side-text">
              <SegmentedText segments={evidence.new_subject_segments} emptyText="No subject entered" />
            </p>
            <p className="side-label">Description</p>
            <p className="side-text">
              <SegmentedText
                segments={evidence.new_description_segments}
                emptyText="No description entered"
              />
            </p>
          </div>
          <div className="side">
            <h5 className="side-heading">Past ticket {id}</h5>
            <p className="side-label">Subject</p>
            <p className="side-text">
              <SegmentedText segments={evidence.past_subject_segments} emptyText={NOT_RECORDED} />
            </p>
            <p className="side-label">Description</p>
            <p className="side-text">
              <SegmentedText
                segments={evidence.past_description_segments}
                emptyText={NOT_RECORDED}
              />
            </p>
          </div>
        </div>
      </section>

      <section className="card-section">
        <h4 className="card-section-heading">Differences in available information</h4>
        <ComparisonTable rows={differences.comparison_rows} ticketId={id} />
        <div className="difference-lists">
          {differences.only_in_new.length > 0 ? (
            <div className="difference-list">
              <p className="list-label">Words in the new ticket that {id} does not use</p>
              <TermList
                terms={differences.only_in_new}
                label={`Words in the new ticket that ${id} does not use`}
              />
            </div>
          ) : null}
          {differences.only_in_past.length > 0 ? (
            <div className="difference-list">
              <p className="list-label">Words in {id} that the new ticket does not use</p>
              <TermList
                terms={differences.only_in_past}
                label={`Words in ${id} that the new ticket does not use`}
              />
            </div>
          ) : null}
        </div>
        {differences.only_in_new.length > 0 || differences.only_in_past.length > 0 ? (
          <p className="basis">
            Exact words only. Other spellings and synonyms are not detected, and a word that is
            absent means not stated, not different.
          </p>
        ) : null}
      </section>

      <section className="card-section check">
        <h4 className="card-section-heading">Check before reusing this resolution</h4>
        {card.verification_questions.length > 0 ? (
          <ul className="question-list">
            {card.verification_questions.map((item) => (
              <li key={item.id}>
                <span className="question">{item.question}</span>
                <span className="basis question-basis">Based on: {item.basis}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <p className="caution">{REUSE_CAUTION}</p>
      </section>

      {referencedIds.length > 0 ? (
        <section className="card-section">
          <h4 className="card-section-heading">Other tickets mentioned in {id}</h4>
          <ul className="referenced-list">
            {referencedIds.map((referencedId) => (
              <ReferencedTicket key={referencedId} ticketId={referencedId} />
            ))}
          </ul>
        </section>
      ) : null}

      <FullRecord ticketId={id} shown={ticket} />

      <details className="disclosure">
        <summary>Technical details (for evaluation)</summary>
        <div className="disclosure-body">
          <p className="basis">{SCORES_NOTE}</p>
          <dl className="record record-compact">
            <dt>Found by</dt>
            <dd>
              {evidence.found_by.length === 0
                ? "neither method"
                : evidence.found_by.map((method) => FOUND_BY_LABELS[method] ?? method).join(", ")}
            </dd>
            <dt>Keyword rank</dt>
            <dd>{formatRank(evidence.keyword_rank)}</dd>
            <dt>Semantic rank</dt>
            <dd>{formatRank(evidence.semantic_rank)}</dd>
            <ScoreRows scores={card.scores} />
          </dl>
        </div>
      </details>

      <FeedbackControls
        ticketId={id}
        verdict={feedback?.verdict ?? null}
        reason={feedback?.reason ?? null}
        onChange={(verdict, reason) => onFeedback(card, verdict, reason)}
      />
    </article>
  );
}
