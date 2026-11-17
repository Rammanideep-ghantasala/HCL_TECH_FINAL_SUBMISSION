import type { Limits, SaveLimits } from "./api/types";

/** A search that has not answered after this long is stopped by the browser. */
export const SEARCH_TIMEOUT_MS = 15_000;

/** A save of a resolved ticket that has not answered after this long is stopped by the browser. */
export const SAVE_TIMEOUT_MS = 20_000;

/** Used when GET /api/meta has no usable escalation address. */
export const FALLBACK_ESCALATION_EMAIL = "ticketexpert@solar.com";

/** Fixed subject of the escalation email link. Ticket text is never put in the link. */
export const ESCALATION_EMAIL_SUBJECT = "Help needed: no historical match";

/** The server's limits for saving a resolved ticket, used when GET /api/meta does not send them. */
export const FALLBACK_SAVE_LIMITS: SaveLimits = {
  subject_max_chars: 200,
  description_max_chars: 4000,
  category_max_chars: 40,
  product_max_chars: 80,
  error_detail_max_chars: 300,
  investigation_max_chars: 3000,
  root_cause_max_chars: 1500,
  resolution_steps_max_chars: 3000,
  verification_max_chars: 1500,
  resolved_by_max_chars: 120,
  max_tags: 10,
  tag_max_chars: 40,
};

/** How many recent searches are remembered in this browser tab. */
export const RECENT_LIMIT = 5;

/** Upper bound on feedback records kept in this browser tab. */
export const FEEDBACK_LIMIT = 200;

/** Upper bound on the size of one sessionStorage entry, in characters. */
export const MAX_STORED_CHARS = 200_000;

/**
 * Used only when GET /api/meta fails. Deliberately small so that a request
 * accepted here is unlikely to be rejected by the server for length.
 */
export const FALLBACK_LIMITS: Limits = {
  subject_max_chars: 150,
  description_max_chars: 2000,
  product_max_chars: 80,
  error_detail_max_chars: 200,
  max_body_bytes: 16_384,
  min_meaningful_terms: 2,
  max_useful_results: 3,
  max_related_results: 3,
};

/** Shown until the server's own notice has loaded, and if it cannot be loaded. */
export const FALLBACK_DATA_NOTICE =
  "Demo with synthetic ticket data. There is no sign-in. Do not paste real customer data.";

export const PRODUCT_NAME = "Support Ticket Similarity Assistant";

export const SESSION_EXPLANATION =
  "Recent searches and feedback are kept in this browser tab only. Nothing is sent to the server, nothing is stored centrally, and nothing retrains the search.";

export const REUSE_CAUTION =
  "A past resolution may describe a one-time product change. Check whether it is something you can repeat for this customer.";

export const STATUS_NOTE = "review status of the fix is not recorded in this dataset";

export const SCORES_NOTE =
  "These are raw retrieval scores for evaluation. They are not probabilities that the fix will work.";

export const FEEDBACK_NOTE = "Useful means worth considering for this ticket, not that the fix worked.";

export const CATEGORY_FILTER_NOTE =
  "A category filter can hide relevant tickets filed under another category. It is never applied automatically.";

export const NOT_RECORDED = "not recorded";

export const EXACT_MATCH_EXPLANATION =
  "The subject and description are identical to a past ticket's, character for character, after trimming surrounding spaces and line endings. This was found by direct comparison, not by a similarity score.";

export const EXACT_MATCH_CAUTION =
  "Identical wording does not mean this is the same incident or that it has the same cause. Each past ticket below is a separate record with its own resolution. Check before reusing it.";
