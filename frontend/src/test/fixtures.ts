/**
 * Invented data for tests. Nothing here comes from the real ticket history,
 * and no test depends on a particular id.
 */
import type {
  ApiError,
  ErrorDetail,
  ExamplesResponse,
  IndexingStatus,
  MetaResponse,
  RefreshIndexResponse,
  ResultCard,
  RetrievalInfo,
  SaveTicketResponse,
  SearchResponse,
  StoredTicket,
  Suggestion,
  TextSegment,
  Ticket,
} from "../api/types";

/** Splits `text` into segments, marking every occurrence of `term`. The pieces always rejoin to `text`. */
export function segmentsOf(text: string, term?: string): TextSegment[] {
  if (!term || !text.includes(term)) {
    return text === "" ? [] : [{ text, match: false }];
  }
  const segments: TextSegment[] = [];
  const pieces = text.split(term);
  pieces.forEach((piece, index) => {
    if (piece !== "") {
      segments.push({ text: piece, match: false });
    }
    if (index < pieces.length - 1) {
      segments.push({ text: term, match: true });
    }
  });
  return segments;
}

export const metaFixture: MetaResponse = {
  app_version: "0.0.0-test",
  data_notice: "Test notice: invented tickets only. No sign-in. Do not paste real customer data.",
  corpus_size: 40,
  rejected_records: 0,
  categories: ["Billing", "Login"],
  limits: {
    subject_max_chars: 40,
    description_max_chars: 400,
    product_max_chars: 30,
    error_detail_max_chars: 60,
    max_body_bytes: 20000,
    min_meaningful_terms: 2,
    max_useful_results: 3,
    max_related_results: 3,
  },
  retrieval: {
    mode: "hybrid",
    degraded: false,
    degraded_reason: null,
    index_version: "idx-test-1",
    model_id: "test-model",
    model_revision: "rev-1",
    relevance_config_version: "rel-test-1",
  },
};

export const examplesFixture: ExamplesResponse = {
  examples: [
    {
      ticket_id: "N-9101",
      subject: "Invented example about a frozen dashboard",
      description: "The invented dashboard freezes after the customer opens the weekly report.",
    },
    {
      ticket_id: "N-9102",
      subject: "Invented example about a missing invoice",
      description: "The customer cannot find the invented invoice for last month.",
    },
  ],
};

export function makeTicket(overrides: Partial<Ticket> = {}): Ticket {
  return {
    ticket_id: "T-9001",
    subject: "Widget sync stops after update",
    description: "After the latest update the widget sync stops with code WX-17.",
    resolution_notes: "Cleared the stale sync token and asked the customer to sign in again.",
    category: "Login",
    status: "resolved",
    tags: ["sync", "widget"],
    created_at: "2031-02-03",
    ...overrides,
  };
}

interface CardOptions {
  ticket?: Partial<Ticket>;
  card?: Partial<Omit<ResultCard, "ticket">>;
  /** A word to highlight in the past ticket and the new ticket. */
  sharedTerm?: string;
  newSubject?: string;
  newDescription?: string;
}

export function makeCard(options: CardOptions = {}): ResultCard {
  const ticket = makeTicket(options.ticket);
  const sharedTerm = options.sharedTerm ?? "sync";
  const newSubject = options.newSubject ?? "Widget sync problem";
  const newDescription = options.newDescription ?? "Customer says the sync never finishes.";
  return {
    result_id: `res-${ticket.ticket_id}`,
    rank: 1,
    match_level: "useful",
    match_label: "Strong textual match",
    ticket,
    evidence: {
      found_by: ["keyword", "semantic"],
      keyword_rank: 1,
      semantic_rank: 2,
      shared_terms: [sharedTerm],
      new_subject_segments: segmentsOf(newSubject, sharedTerm),
      new_description_segments: segmentsOf(newDescription, sharedTerm),
      past_subject_segments: segmentsOf(ticket.subject, sharedTerm),
      past_description_segments: segmentsOf(ticket.description, sharedTerm),
      excerpts: [],
      retrieval_note: "Found by keyword overlap and by meaning. Highlights mark shared words.",
    },
    differences: {
      only_in_new: ["finishes"],
      only_in_past: ["token"],
      unseen_in_history: [],
      comparison_rows: [
        {
          key: "product_or_system",
          label: "Product or system",
          new_values: [],
          past_values: ["Widget"],
          new_basis: "not entered",
          past_basis: "detected in text",
          outcome: "unknown",
          statement: "The new ticket does not name a product, so this cannot be compared.",
        },
        {
          key: "error_code",
          label: "Error code",
          new_values: ["WX-20"],
          past_values: ["WX-17"],
          new_basis: "entered by you",
          past_basis: "detected in text",
          outcome: "different",
          statement: "The error codes differ.",
        },
      ],
    },
    verification_questions: [
      {
        id: "vq-1",
        question: "Does the customer see code WX-17 as the past ticket did?",
        basis: "error code found in the past ticket description",
      },
    ],
    referenced_ticket_ids: [],
    scores: { keyword_cosine: 0.41234, semantic_cosine: 0.6789, query_coverage: 0.4321, rrf: 0.0325 },
    ...options.card,
  };
}

export function makeRetrieval(overrides: Partial<RetrievalInfo> = {}): RetrievalInfo {
  return {
    mode: "hybrid",
    degraded: false,
    degraded_reason: null,
    keyword_signal: true,
    category_filter: null,
    hidden_by_filter: 0,
    semantic_windows: 1,
    corpus_size: 40,
    index_version: "idx-test-1",
    model_id: "test-model",
    model_revision: "rev-1",
    relevance_config_version: "rel-test-1",
    ...overrides,
  };
}

export function makeSuggestion(overrides: Partial<Suggestion> = {}): Suggestion {
  return {
    id: "sg-1",
    message: "Add the exact error code if the customer gave one.",
    basis: "no error code was found in the new ticket",
    field: "error_detail",
    ...overrides,
  };
}

export function makeResponse(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    request_id: "req-test-1",
    query_fingerprint: "fp-test-1",
    status: "useful_matches",
    message: "1 past ticket looks useful for this ticket.",
    results: [makeCard()],
    related: [],
    query_insights: {
      meaningful_term_count: 4,
      detected_names: [],
      detected_codes: [],
      unseen_terms: [],
      suggestions: [],
    },
    retrieval: makeRetrieval(),
    timing_ms: { total: 12, keyword: 3, semantic: 8 },
    ...overrides,
  };
}

export function makeNoStrongMatch(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return makeResponse({
    request_id: "req-test-nsm",
    status: "no_strong_match",
    message: "No past ticket is close enough to suggest.",
    results: [],
    related: [],
    query_insights: {
      meaningful_term_count: 3,
      detected_names: [],
      detected_codes: [],
      unseen_terms: ["zorblat"],
      suggestions: [makeSuggestion()],
    },
    ...overrides,
  });
}

export function makeInsufficientInput(overrides: Partial<SearchResponse> = {}): SearchResponse {
  return makeResponse({
    request_id: "req-test-ins",
    status: "insufficient_input",
    message: "This ticket is too short to search reliably.",
    results: [],
    related: [],
    query_insights: {
      meaningful_term_count: 1,
      detected_names: [],
      detected_codes: [],
      unseen_terms: [],
      suggestions: [makeSuggestion()],
    },
    ...overrides,
  });
}

export function makeStoredTicket(overrides: Partial<StoredTicket> = {}): StoredTicket {
  return {
    ...makeTicket(),
    revision: "rev-test-1",
    indexed: true,
    ...overrides,
  };
}

export function makeIndexing(overrides: Partial<IndexingStatus> = {}): IndexingStatus {
  return {
    status: "indexed",
    searchable: true,
    message: "The ticket was added to the search index.",
    index_version: "idx-test-2",
    ...overrides,
  };
}

/** Indexing that failed after the ticket itself was saved. */
export function makeFailedIndexing(overrides: Partial<IndexingStatus> = {}): IndexingStatus {
  return makeIndexing({
    status: "failed",
    searchable: false,
    message: "The invented search index could not be updated right now.",
    index_version: "idx-test-1",
    ...overrides,
  });
}

export function makeSaveResponse(overrides: Partial<SaveTicketResponse> = {}): SaveTicketResponse {
  return {
    outcome: "created",
    repeat_submission: false,
    ticket: makeStoredTicket({ ticket_id: "T-9101" }),
    indexing: makeIndexing(),
    message: "Invented ticket T-9101 was created.",
    ...overrides,
  };
}

export function makeRefreshResponse(
  overrides: Partial<RefreshIndexResponse> = {},
): RefreshIndexResponse {
  return {
    indexing: makeIndexing(),
    unindexed_ticket_ids: [],
    ...overrides,
  };
}

export function makeApiError(
  code: string,
  message: string,
  details: ErrorDetail[] = [],
  requestId: string | null = "req-err-7",
): ApiError {
  return { error: { code, message, request_id: requestId, details } };
}
