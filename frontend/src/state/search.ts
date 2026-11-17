/**
 * Types and pure helpers for the ticket form and the search area.
 * No React and no I/O in this file.
 */
import type { ApiFailure } from "../api/client";
import type { Limits, SearchRequest, SearchResponse, SearchStatus } from "../api/types";

/** The ticket-entry form. `category` is "" when no filter is chosen. */
export interface FormValues {
  top_k?: 3 | 5;
  subject: string;
  description: string;
  product: string;
  error_detail: string;
  category: string;
}

export type FieldName = Exclude<keyof FormValues, "top_k">;

export const FIELD_NAMES: readonly FieldName[] = [
  "subject",
  "description",
  "product",
  "error_detail",
  "category",
];

export const FIELD_LABELS: Record<FieldName, string> = {
  subject: "Subject",
  description: "Description",
  product: "Product or system",
  error_detail: "Error code or message",
  category: "Category filter",
};

export const EMPTY_FORM: FormValues = {
  top_k: 3,
  subject: "",
  description: "",
  product: "",
  error_detail: "",
  category: "",
};

export type FieldErrors = Partial<Record<FieldName, string>>;

export type FailureKind = "timeout" | "network" | "server" | "rejected";

/** The whole search area is always in exactly one of these states. */
export type SearchState =
  | { phase: "idle" }
  | { phase: "loading"; submitted: FormValues }
  | { phase: "done"; submitted: FormValues; response: SearchResponse }
  | {
      phase: "failed";
      submitted: FormValues;
      kind: FailureKind;
      message: string;
      requestId: string | null;
    };

const KNOWN_STATUSES: readonly string[] = [
  "useful_matches",
  "no_strong_match",
  "insufficient_input",
] satisfies readonly SearchStatus[];

export function isKnownStatus(status: string): status is SearchStatus {
  return KNOWN_STATUSES.includes(status);
}

export function toPayload(values: FormValues): SearchRequest {
  const product = values.product.trim();
  const errorDetail = values.error_detail.trim();
  return {
    top_k: values.top_k ?? 3,
    subject: values.subject.trim(),
    description: values.description.trim(),
    product: product === "" ? null : product,
    error_detail: errorDetail === "" ? null : errorDetail,
    category: values.category === "" ? null : values.category,
  };
}

export function sameValues(a: FormValues, b: FormValues): boolean {
  return (a.top_k ?? 3) === (b.top_k ?? 3) && FIELD_NAMES.every((name) => a[name] === b[name]);
}

const SUMMARY_WORDS = 12;

/** Short label for a submitted ticket: its subject, or the first words of its description. */
export function summariseInput(values: Pick<FormValues, "subject" | "description">): string {
  const subject = values.subject.trim();
  if (subject !== "") {
    return subject;
  }
  const words = values.description.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return "(no subject or description)";
  }
  const head = words.slice(0, SUMMARY_WORDS).join(" ");
  return words.length > SUMMARY_WORDS ? `${head}…` : head;
}

const LIMIT_BY_FIELD: Partial<Record<FieldName, keyof Limits>> = {
  subject: "subject_max_chars",
  description: "description_max_chars",
  product: "product_max_chars",
  error_detail: "error_detail_max_chars",
};

export function limitFor(field: FieldName, limits: Limits): number | null {
  const key = LIMIT_BY_FIELD[field];
  return key ? limits[key] : null;
}

const EMPTY_SUBJECT_MESSAGE = "Enter a subject or a description of the problem. Both are empty.";
const EMPTY_DESCRIPTION_MESSAGE = "Describe the problem here, or enter a subject above.";

/** Minimal client-side checks. The server remains the authority. */
export function validateForm(values: FormValues, limits: Limits): FieldErrors {
  const errors: FieldErrors = {};
  if (values.subject.trim() === "" && values.description.trim() === "") {
    errors.subject = EMPTY_SUBJECT_MESSAGE;
    errors.description = EMPTY_DESCRIPTION_MESSAGE;
  }
  for (const field of FIELD_NAMES) {
    const max = limitFor(field, limits);
    const length = values[field].length;
    if (max !== null && length > max) {
      errors[field] =
        `${FIELD_LABELS[field]} is ${length - max} characters over the limit of ${max}. Shorten it and search again.`;
    }
  }
  return errors;
}

/**
 * Errors to keep after `edited` changed: that field's own message goes, and so
 * does the "both are empty" pair once either of the two fields is edited.
 */
export function errorsAfterEdit(errors: FieldErrors, edited: FieldName): FieldErrors {
  const next: FieldErrors = { ...errors };
  delete next[edited];
  if (edited === "subject" || edited === "description") {
    if (next.subject === EMPTY_SUBJECT_MESSAGE) {
      delete next.subject;
    }
    if (next.description === EMPTY_DESCRIPTION_MESSAGE) {
      delete next.description;
    }
  }
  return next;
}

export function firstInvalidField(errors: FieldErrors): FieldName | null {
  return FIELD_NAMES.find((name) => errors[name] !== undefined) ?? null;
}

function toFieldName(raw: string | null): FieldName | null {
  if (raw === null) {
    return null;
  }
  // Accept "description" as well as dotted locations such as "body.description".
  const last = raw.split(".").pop() ?? raw;
  return FIELD_NAMES.find((name) => name === last) ?? null;
}

const FALLBACK_FIELD_BY_CODE: Record<string, FieldName> = {
  empty_input: "subject",
  unknown_category: "category",
};

/**
 * Splits a rejected request into messages that belong to a form field and
 * messages that do not.
 */
export function mapRejection(failure: ApiFailure): { fieldErrors: FieldErrors; other: string[] } {
  const fieldErrors: FieldErrors = {};
  const other: string[] = [];
  for (const detail of failure.details) {
    const field = toFieldName(detail.field);
    if (field === null) {
      other.push(detail.message);
    } else if (fieldErrors[field] === undefined) {
      fieldErrors[field] = detail.message;
    } else {
      fieldErrors[field] = `${fieldErrors[field]} ${detail.message}`;
    }
  }
  if (Object.keys(fieldErrors).length === 0 && failure.code !== null && failure.message !== "") {
    const fallback = FALLBACK_FIELD_BY_CODE[failure.code];
    if (fallback) {
      fieldErrors[fallback] = failure.message;
    }
  }
  return { fieldErrors, other };
}

const DEFAULT_FAILURE_MESSAGES: Record<FailureKind, string> = {
  timeout: "The search took too long and was stopped before an answer arrived.",
  network: "The search service could not be reached. Check your connection.",
  server: "The search service reported a problem and did not return results.",
  rejected: "The server did not accept this ticket as entered.",
};

export function defaultFailureMessage(kind: FailureKind): string {
  return DEFAULT_FAILURE_MESSAGES[kind];
}

/** Text for the page's single live region. It differs for every search state. */
export function announcementFor(state: SearchState): string {
  switch (state.phase) {
    case "idle":
      return "No search is showing.";
    case "loading":
      return "Searching past tickets…";
    case "failed":
      return `The search did not complete. ${state.message}`;
    case "done": {
      const { response } = state;
      const exactCount = response.results.filter((card) => card.match_level === "exact").length;
      if (response.status === "useful_matches" && exactCount > 0) {
        return `Search finished. Exact text match found: ${exactCount} past ${exactCount === 1 ? "ticket" : "tickets"} with identical wording shown.`;
      }
      if (response.status === "useful_matches") {
        const count = response.results.length;
        return `Search finished. ${count} useful ${count === 1 ? "match" : "matches"} shown.`;
      }
      if (response.status === "no_strong_match") {
        return "Search finished. No relevant historical match found. Human investigation is required. This is a search result, not an error.";
      }
      return "Not searched. More detail is needed.";
    }
  }
}
