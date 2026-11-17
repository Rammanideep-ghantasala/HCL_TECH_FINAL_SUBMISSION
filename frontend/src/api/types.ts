/**
 * API contract between the React frontend and the FastAPI backend.
 *
 * This file mirrors backend/app/schemas.py. JSON field names are snake_case
 * on the wire, so they are snake_case here too. Change both sides together.
 */

/** Overall outcome of a search that ran without a service error. */
export type SearchStatus =
  /** At least one past ticket passed the "useful" thresholds. */
  | "useful_matches"
  /** Nothing passed the "useful" thresholds. `related` may still hold context. */
  | "no_strong_match"
  /** The input was valid but too vague to search. No search was run. */
  | "insufficient_input";

export type RetrievalMode = "hybrid" | "keyword_only" | "semantic_only";

/**
 * "exact": the subject and description are identical to a past ticket's
 * (after trimming surrounding whitespace and normalizing line endings).
 * It says nothing about whether it is the same incident.
 */
export type MatchLevel = "exact" | "useful" | "related";

/** Body of POST /api/search. */
export interface SearchRequest {
  top_k?: 3 | 5;
  subject: string;
  description: string;
  /** Optional refinement: affected product, integration or feature. */
  product?: string | null;
  /** Optional refinement: exact error code or error message. */
  error_detail?: string | null;
  /** Optional explicit filter. Must be one of MetaResponse.categories. */
  category?: string | null;
  /** Optional id of the new ticket. Only looked up; a search never saves it. */
  ticket_id?: string | null;
}

/** A run of original text. `match` marks keyword overlap with the other ticket. */
export interface TextSegment {
  text: string;
  match: boolean;
}

/** A historical ticket exactly as stored. Nothing here is generated. */
export interface Ticket {
  ticket_id: string;
  subject: string;
  description: string;
  resolution_notes: string;
  /** null means the source record has no value. */
  category: string | null;
  status: string | null;
  tags: string[];
  /** Date string as recorded in the source (YYYY-MM-DD), or null. */
  created_at: string | null;
  // The next seven fields are recorded through the resolution form and are null
  // (or absent) on tickets that predate it. For those tickets resolution_notes
  // holds the resolution steps.
  /** Product or version recorded with the resolution. */
  product?: string | null;
  /** Error details recorded with the resolution. */
  error_detail?: string | null;
  /** What the agent checked and found. */
  investigation?: string | null;
  /** Root cause as recorded; may be "Unknown". */
  root_cause?: string | null;
  /** How the agent confirmed the issue was resolved. */
  verification?: string | null;
  /** Who resolved it (the signed-in identity when one is configured). */
  resolved_by?: string | null;
  /** ISO 8601 UTC time set by the server when the resolution was saved. */
  resolved_at?: string | null;
}

/** GET /api/tickets/{ticket_id}: the stored ticket plus its save state. */
export interface StoredTicket extends Ticket {
  /** Changes whenever the stored record changes. Send it back to replace this version. */
  revision: string;
  /** True when this exact revision is in the live search index. */
  indexed: boolean;
  /**
   * Indexing state recorded in the ticket store: "pending" (saved, not yet
   * confirmed searchable), "indexed" (saved and searchable), "failed"
   * (saved; the last indexing attempt failed) or "rejected" (saved, but the
   * text failed validation for indexing; it stays out of search results
   * until its content changes, so retrying does not help). Seed tickets
   * report "indexed".
   */
  index_status?: "pending" | "indexed" | "failed" | "rejected" | null;
}

/**
 * One row of the "available information" comparison.
 * An empty values array means unknown. Unknown is never a mismatch.
 */
export interface ComparisonRow {
  key: "product_or_system" | "error_code" | "category" | "tags" | "created_at" | "status";
  label: string;
  new_values: string[];
  past_values: string[];
  /** Where the new-ticket value came from, e.g. "entered by you". */
  new_basis: string;
  /** Where the past-ticket value came from, e.g. "recorded field". */
  past_basis: string;
  outcome: "same" | "different" | "partly_shared" | "unknown";
  /** Deterministic sentence describing the row. */
  statement: string;
}

export interface Excerpt {
  field: "subject" | "description";
  segments: TextSegment[];
}

export interface Evidence {
  found_by: Array<"keyword" | "semantic" | "exact_text">;
  keyword_rank: number | null;
  semantic_rank: number | null;
  /** Distinctive words or two-word phrases present in both tickets. */
  shared_terms: string[];
  new_subject_segments: TextSegment[];
  new_description_segments: TextSegment[];
  past_subject_segments: TextSegment[];
  past_description_segments: TextSegment[];
  /** The past ticket's sentences that carry the most shared wording. */
  excerpts: Excerpt[];
  /** Plain statement of how the ticket was found and what highlights mean. */
  retrieval_note: string;
}

export interface Differences {
  /** Distinctive terms in the new ticket that this past ticket never uses. */
  only_in_new: string[];
  /** Distinctive terms in the past ticket's problem text absent from the new ticket. */
  only_in_past: string[];
  /** Terms in the new ticket that appear in no historical ticket at all. */
  unseen_in_history: string[];
  comparison_rows: ComparisonRow[];
}

export interface VerificationQuestion {
  id: string;
  question: string;
  /** What the question is grounded in (a source field or a missing field). */
  basis: string;
}

/** Raw retrieval numbers kept for evaluation. They are not probabilities. */
export interface Scores {
  keyword_cosine: number | null;
  semantic_cosine: number | null;
  /** Share of the new ticket's wording (weighted by rarity) found in the past ticket. */
  query_coverage: number | null;
  rrf: number | null;
}

export interface ResultCard {
  /** Stable for the same query, ticket and index version. */
  result_id: string;
  rank: number;
  match_level: MatchLevel;
  /** Human label for match_level, e.g. "Strong textual match". */
  match_label: string;
  ticket: Ticket;
  evidence: Evidence;
  differences: Differences;
  verification_questions: VerificationQuestion[];
  /**
   * IDs of other historical tickets that this ticket's own text mentions,
   * found by matching known ticket IDs. Each can be opened with
   * GET /api/tickets/{ticket_id}.
   */
  referenced_ticket_ids: string[];
  scores: Scores;
}

export interface Suggestion {
  id: string;
  message: string;
  basis: string;
  /** Form field the suggestion refers to, when there is one. */
  field: "subject" | "description" | "product" | "error_detail" | "category" | null;
}

export interface QueryInsights {
  meaningful_term_count: number;
  /** Product or system names found in the new ticket (entered or detected). */
  detected_names: string[];
  /** Error codes found in the new ticket (entered or detected). */
  detected_codes: string[];
  /** Terms in the new ticket that appear in no historical ticket. */
  unseen_terms: string[];
  suggestions: Suggestion[];
}

/** Outcome of the exact-text check that runs before any similarity search. */
export interface ExactMatchInfo {
  ticket_id: string | null;
  ticket_id_status: "not_supplied" | "not_found" | "exists_same_text" | "exists_different_text";
  /** Every past ticket whose subject and description are exactly the submitted text. */
  matched_ticket_ids: string[];
  /** Notice about the supplied ticket id, safe to show as plain text. */
  message: string | null;
}

export interface RetrievalInfo {
  /** False when the answer came from the exact-text check or the input was too vague. Absent from older servers. */
  similarity_search_run?: boolean;
  mode: RetrievalMode;
  /** True when a retrieval method was unavailable or failed for this request. */
  degraded: boolean;
  degraded_reason: string | null;
  /** False when the query shares no known term with the history. */
  keyword_signal: boolean;
  category_filter: string | null;
  /** Useful-level tickets in other categories hidden by the category filter. */
  hidden_by_filter: number;
  /** How many windows the semantic model needed to cover the whole input. */
  semantic_windows: number;
  corpus_size: number;
  index_version: string;
  model_id: string | null;
  model_revision: string | null;
  relevance_config_version: string;
}

export interface Timing {
  total: number;
  keyword: number | null;
  semantic: number | null;
}

/** 200 response of POST /api/search. */
export interface SearchResponse {
  top_k?: 3 | 5;
  summary_token?: string | null;
  request_id: string;
  /** Hash of the normalized query and filter. Contains no ticket text. */
  query_fingerprint: string;
  status: SearchStatus;
  /** Server-written summary of the outcome, safe to show as plain text. */
  message: string;
  /** Up to three useful candidates. May be empty. Never padded. */
  results: ResultCard[];
  /** Related context that did not pass the "useful" thresholds. */
  related: ResultCard[];
  /** Always sent by this backend; optional so older responses still render. */
  exact_match?: ExactMatchInfo;
  query_insights: QueryInsights;
  retrieval: RetrievalInfo;
  timing_ms: Timing;
}

export interface ErrorDetail {
  field: string | null;
  message: string;
}

/**
 * Body of every non-2xx API response.
 * Codes: validation_error (422), empty_input (422), unknown_category (422),
 * request_too_large (413), ticket_not_found (404), not_found (404),
 * save_disabled (403), ticket_conflict (409), submission_conflict (409),
 * save_conflict (409), busy (503), search_unavailable (503),
 * database_unavailable (503), database_schema_missing (503),
 * database_permission_denied (503), ticket_store_unavailable (503),
 * store_unavailable (503), store_full (503), internal_error (500).
 * A 422 validation_error also covers text containing control characters;
 * its details[].field names the field.
 * A 409 ticket_conflict body does not carry the stored revision; fetch
 * GET /api/tickets/{id} to show the stored version and get its revision.
 */
export interface ApiError {
  error: {
    code: string;
    message: string;
    request_id: string | null;
    details: ErrorDetail[];
  };
}

export interface ExampleTicket {
  ticket_id: string;
  subject: string;
  description: string;
}

/** GET /api/examples */
export interface ExamplesResponse {
  examples: ExampleTicket[];
}

export interface Limits {
  subject_max_chars: number;
  description_max_chars: number;
  product_max_chars: number;
  error_detail_max_chars: number;
  max_body_bytes: number;
  min_meaningful_terms: number;
  max_useful_results: number;
  max_related_results: number;
}

/** GET /api/meta */
export interface MetaResponse {
  app_version: string;
  /** Notice about the data and access model, shown in the UI. */
  data_notice: string;
  corpus_size: number;
  rejected_records: number;
  categories: string[];
  limits: Limits;
  /** False when saving resolved tickets is switched off or the store is unusable. */
  save_enabled?: boolean;
  save_limits?: SaveLimits;
  /** Address to contact when no historical match exists. */
  escalation_email?: string;
  /** Authenticated user from a trusted proxy header, when one is configured. */
  identity?: string | null;
  unindexed_ticket_ids?: string[];
  /** Saved tickets whose text failed validation for indexing; not indexable until their content changes. */
  rejected_ticket_ids?: string[];
  /** Whether on-demand AI summaries are available. Never carries the API key. */
  summarization?: SummarizationInfo;
  retrieval: {
    mode: RetrievalMode | "unavailable";
    degraded: boolean;
    degraded_reason: string | null;
    index_version: string | null;
    model_id: string | null;
    model_revision: string | null;
    relevance_config_version: string;
  };
}

/** GET /api/tickets/{ticket_id} returns the stored ticket with its revision. */
export type TicketResponse = StoredTicket;

/**
 * Body of POST /api/tickets: save a resolved ticket.
 *
 * Errors: 422 validation_error (details[].field is one of these keys),
 * 409 ticket_conflict (ticket_id exists with different content and
 * expected_revision is missing or stale; nothing was saved),
 * 409 submission_conflict (this submission_id was already saved with other content),
 * 403 save_disabled, 503 store_unavailable, 503 store_full,
 * 503 database_permission_denied (the server may not write to its database).
 */
export interface SaveTicketRequest {
  /** One per resolution form. Repeating it never creates a second record. */
  submission_id: string;
  /** The ticket's id. Omit to have the server generate a new one. */
  ticket_id?: string | null;
  /** Required to change an existing ticket: the revision you reviewed. */
  expected_revision?: string | null;
  subject: string;
  description: string;
  category: string;
  product?: string | null;
  error_detail?: string | null;
  /** What was checked and what was found. */
  investigation: string;
  /** "Unknown" is accepted. */
  root_cause?: string | null;
  /** The actions that fixed the issue. Stored as resolution_notes. */
  resolution_steps: string;
  /** How the agent confirmed the issue was resolved. */
  verification: string;
  /** Ignored when the server has an authenticated identity (see MetaResponse.identity). */
  resolved_by: string;
  tags?: string[];
  /** Must be true. */
  confirm_resolved: boolean;
}

/**
 * Outcome of the indexing attempt that followed a save or a refresh. It
 * describes the whole search index, not one ticket: after a refresh, check
 * RefreshIndexResponse.unindexed_ticket_ids (or StoredTicket.indexed) for a
 * particular ticket.
 */
export interface IndexingStatus {
  /** "indexed": the new index was published. "failed": saved data is kept, the previous index still serves searches. */
  status: "indexed" | "failed";
  /** Always true when status is "indexed" and false when it is "failed". */
  searchable: boolean;
  /** Server-written, safe to show as plain text. */
  message: string;
  index_version: string | null;
}

/** 201 (created) or 200 (updated or unchanged) from POST /api/tickets. */
export interface SaveTicketResponse {
  outcome: "created" | "updated" | "unchanged";
  /** True when this submission_id had already been saved; nothing new was written. */
  repeat_submission: boolean;
  ticket: StoredTicket;
  indexing: IndexingStatus;
  message: string;
}

/** 200 from POST /api/index/refresh (the retry path after an indexing failure). */
export interface RefreshIndexResponse {
  indexing: IndexingStatus;
  /** Saved tickets whose current revision is not yet in the search index. */
  unindexed_ticket_ids: string[];
  /**
   * Saved tickets whose text failed validation for indexing. They stay out of
   * search results until their content changes. Absent from older servers.
   */
  rejected_ticket_ids?: string[];
}

/** GET /api/meta summarization block. */
export interface SummarizationInfo {
  /** True when the server has an API key configured. Says nothing about whether the key works. */
  configured: boolean;
  provider: "openai";
  model: string;
  /** Server-written notice that the selected ticket's content is sent to the provider. */
  data_notice: string;
}

/** POST /api/summaries; exact ordered main-match IDs from a completed search. */
export interface SummaryRequest {
  summary_token: string;
  ticket_ids: string[];
}

export interface SummaryResponse {
  ticket_ids: string[];
  source_revisions: string[];
  model: string;
  prompt_version: string;
  generated_at: string;
  summary: {
    overview: Array<{ text: string; ticket_ids: string[] }>;
    tickets: Array<{
      ticket_id: string;
      reported_issue: string;
      recorded_resolution: string;
      applicability_notes: string[];
    }>;
  };
  disclaimer: string;
}

export interface SaveLimits {
  subject_max_chars: number;
  description_max_chars: number;
  category_max_chars: number;
  product_max_chars: number;
  error_detail_max_chars: number;
  investigation_max_chars: number;
  root_cause_max_chars: number;
  resolution_steps_max_chars: number;
  verification_max_chars: number;
  resolved_by_max_chars: number;
  max_tags: number;
  tag_max_chars: number;
}

/** GET /health (liveness). */
export interface HealthResponse {
  status: "alive";
}

/** GET /ready (readiness). 200 when ready, 503 when not. */
export interface ReadyResponse {
  status: "ready" | "not_ready";
  mode: RetrievalMode | "unavailable";
  degraded: boolean;
  corpus_size: number;
  index_version: string | null;
  checks: {
    tickets: boolean;
    keyword: boolean;
    semantic: boolean;
  };
}
