/**
 * Types and pure helpers for recording how a ticket with no historical match
 * was resolved. No React and no I/O in this file.
 */
import type { ApiFailure } from "../api/client";
import type { MetaResponse, SaveLimits, SaveTicketRequest, Ticket } from "../api/types";
import {
  ESCALATION_EMAIL_SUBJECT,
  FALLBACK_ESCALATION_EMAIL,
  FALLBACK_SAVE_LIMITS,
} from "../constants";
import type { FormValues } from "./search";

/** The resolution form as typed. `tags` is the comma-separated text. */
export interface ResolutionValues {
  ticket_id: string;
  subject: string;
  description: string;
  category: string;
  product: string;
  error_detail: string;
  investigation: string;
  root_cause: string;
  resolution_steps: string;
  verification: string;
  resolved_by: string;
  tags: string;
  confirm_resolved: boolean;
}

export type ResolutionField = keyof ResolutionValues;
export type ResolutionTextField = Exclude<ResolutionField, "confirm_resolved">;

/** Display order. The first invalid field in this order receives focus. */
export const RESOLUTION_FIELDS: readonly ResolutionField[] = [
  "ticket_id",
  "subject",
  "description",
  "category",
  "product",
  "error_detail",
  "investigation",
  "root_cause",
  "resolution_steps",
  "verification",
  "resolved_by",
  "tags",
  "confirm_resolved",
];

export const RESOLUTION_LABELS: Record<ResolutionField, string> = {
  ticket_id: "Ticket ID",
  subject: "Subject",
  description: "Description",
  category: "Category",
  product: "Product or version",
  error_detail: "Error details",
  investigation: "Investigation approach",
  root_cause: "Root cause",
  resolution_steps: "Resolution steps",
  verification: "Verification",
  resolved_by: "Resolved by",
  tags: "Tags",
  confirm_resolved: "I confirm this issue has been resolved",
};

const REQUIRED_TEXT_FIELDS: readonly ResolutionTextField[] = [
  "subject",
  "description",
  "category",
  "investigation",
  "resolution_steps",
  "verification",
  "resolved_by",
];

export function isRequired(field: ResolutionField): boolean {
  return field === "confirm_resolved" || (REQUIRED_TEXT_FIELDS as readonly string[]).includes(field);
}

export type ResolutionErrors = Partial<Record<ResolutionField, string>>;

export const UNKNOWN_ROOT_CAUSE = "Unknown";

const TICKET_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
/** Shared by the category and by each tag. */
const LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$/;

const LIMIT_KEYS: Partial<Record<ResolutionTextField, keyof SaveLimits>> = {
  subject: "subject_max_chars",
  description: "description_max_chars",
  category: "category_max_chars",
  product: "product_max_chars",
  error_detail: "error_detail_max_chars",
  investigation: "investigation_max_chars",
  root_cause: "root_cause_max_chars",
  resolution_steps: "resolution_steps_max_chars",
  verification: "verification_max_chars",
  resolved_by: "resolved_by_max_chars",
};

export function resolutionLimitFor(field: ResolutionTextField, limits: SaveLimits): number | null {
  const key = LIMIT_KEYS[field];
  return key ? limits[key] : null;
}

/** The server's save limits, with the built-in value for any that are missing or unusable. */
export function saveLimitsFrom(meta: MetaResponse | null): SaveLimits {
  const sent = meta?.save_limits;
  if (!sent) {
    return FALLBACK_SAVE_LIMITS;
  }
  const merged: SaveLimits = { ...FALLBACK_SAVE_LIMITS };
  for (const key of Object.keys(FALLBACK_SAVE_LIMITS) as Array<keyof SaveLimits>) {
    const value: unknown = sent[key];
    if (typeof value === "number" && Number.isInteger(value) && value > 0) {
      merged[key] = value;
    }
  }
  return merged;
}

/** The signed-in identity reported by the server, or null when there is none. */
export function identityFrom(meta: MetaResponse | null): string | null {
  const identity = meta?.identity;
  return typeof identity === "string" && identity.trim() !== "" ? identity.trim() : null;
}

/** A plain address only: anything that could add parameters to the link is refused. */
const PLAIN_EMAIL = /^[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;

export function escalationAddress(meta: MetaResponse | null): string {
  const address = meta?.escalation_email?.trim() ?? "";
  return PLAIN_EMAIL.test(address) ? address : FALLBACK_ESCALATION_EMAIL;
}

/** The escalation link. Its subject is fixed text; ticket text is never added. */
export function escalationMailto(address: string): string {
  return `mailto:${address}?subject=${encodeURIComponent(ESCALATION_EMAIL_SUBJECT)}`;
}

/** A new draft, filled from the snapshot that was searched (never from the live search form). */
export function initialResolutionValues(
  submitted: FormValues,
  identity: string | null,
): ResolutionValues {
  return {
    ticket_id: "",
    subject: submitted.subject.trim(),
    description: submitted.description.trim(),
    category: submitted.category,
    product: submitted.product.trim(),
    error_detail: submitted.error_detail.trim(),
    investigation: "",
    root_cause: "",
    resolution_steps: "",
    verification: "",
    resolved_by: identity ?? "",
    tags: "",
    confirm_resolved: false,
  };
}

/** Comma-separated text to tags: trimmed, empty pieces dropped, repeats removed. */
export function parseTags(text: string): string[] {
  const tags: string[] = [];
  for (const piece of text.split(",")) {
    const tag = piece.trim();
    if (tag !== "" && !tags.includes(tag)) {
      tags.push(tag);
    }
  }
  return tags;
}

function overLimit(label: string, length: number, max: number): string {
  return `${label} is ${length - max} characters over the limit of ${max}. Shorten it.`;
}

function validateTags(text: string, limits: SaveLimits): string | undefined {
  const tags = parseTags(text);
  if (tags.length > limits.max_tags) {
    return `Use at most ${limits.max_tags} tags. ${tags.length} were entered.`;
  }
  for (const tag of tags) {
    if (tag.length > limits.tag_max_chars) {
      return `The tag "${tag}" is ${tag.length - limits.tag_max_chars} characters over the limit of ${limits.tag_max_chars}.`;
    }
    if (!LABEL_PATTERN.test(tag)) {
      return `The tag "${tag}" may use only letters, digits, spaces, dots, hyphens and underscores, and must start with a letter or digit.`;
    }
  }
  return undefined;
}

/**
 * Client checks that mirror the server's rules. The server remains the
 * authority. With a signed-in identity, "Resolved by" is not the agent's to fill.
 */
export function validateResolution(
  values: ResolutionValues,
  limits: SaveLimits,
  identity: string | null,
): ResolutionErrors {
  const errors: ResolutionErrors = {};

  const ticketId = values.ticket_id.trim();
  if (ticketId !== "" && !TICKET_ID_PATTERN.test(ticketId)) {
    errors.ticket_id =
      "Use up to 64 letters, digits, dots, hyphens or underscores, starting with a letter or digit, or leave it blank.";
  }

  for (const field of REQUIRED_TEXT_FIELDS) {
    if (field === "resolved_by" && identity !== null) {
      continue;
    }
    if (values[field].trim() === "") {
      errors[field] = `${RESOLUTION_LABELS[field]} is required.`;
    }
  }

  for (const field of Object.keys(LIMIT_KEYS) as ResolutionTextField[]) {
    const max = resolutionLimitFor(field, limits);
    const length = values[field].length;
    if (max !== null && length > max && !(field === "resolved_by" && identity !== null)) {
      errors[field] = overLimit(RESOLUTION_LABELS[field], length, max);
    }
  }

  const category = values.category.trim();
  if (errors.category === undefined && category !== "" && !LABEL_PATTERN.test(category)) {
    errors.category =
      "Use up to 40 letters, digits, spaces, dots, hyphens or underscores, starting with a letter or digit.";
  }

  const tagError = validateTags(values.tags, limits);
  if (tagError) {
    errors.tags = tagError;
  }

  if (!values.confirm_resolved) {
    errors.confirm_resolved =
      "Tick this box to confirm the issue has been resolved. Only resolved tickets are saved.";
  }
  return errors;
}

export function firstInvalidResolutionField(errors: ResolutionErrors): ResolutionField | null {
  return RESOLUTION_FIELDS.find((field) => errors[field] !== undefined) ?? null;
}

/** Errors to keep after `edited` changed. */
export function resolutionErrorsAfterEdit(
  errors: ResolutionErrors,
  edited: ResolutionField,
): ResolutionErrors {
  const next: ResolutionErrors = { ...errors };
  delete next[edited];
  return next;
}

function optionalText(text: string): string | null {
  const trimmed = text.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * The exact body of POST /api/tickets. `expectedRevision` is sent only when
 * the agent chose to replace a stored version.
 */
export function toSaveRequest(
  values: ResolutionValues,
  submissionId: string,
  expectedRevision: string | null,
  identity: string | null,
): SaveTicketRequest {
  const request: SaveTicketRequest = {
    submission_id: submissionId,
    ticket_id: optionalText(values.ticket_id),
    subject: values.subject.trim(),
    description: values.description.trim(),
    category: values.category.trim(),
    product: optionalText(values.product),
    error_detail: optionalText(values.error_detail),
    investigation: values.investigation.trim(),
    root_cause: optionalText(values.root_cause),
    resolution_steps: values.resolution_steps.trim(),
    verification: values.verification.trim(),
    resolved_by: (identity ?? values.resolved_by).trim(),
    tags: parseTags(values.tags),
    confirm_resolved: values.confirm_resolved,
  };
  if (expectedRevision !== null) {
    request.expected_revision = expectedRevision;
  }
  return request;
}

/** Accepts "category" as well as locations such as "body.category" or "body.tags.2". */
function toResolutionField(raw: string | null): ResolutionField | null {
  if (raw === null) {
    return null;
  }
  const parts = raw.split(".");
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    const match = RESOLUTION_FIELDS.find((field) => field === parts[index]);
    if (match) {
      return match;
    }
  }
  return null;
}

/** Splits a rejected save into messages for form fields and messages that belong to none. */
export function mapSaveRejection(failure: ApiFailure): {
  fieldErrors: ResolutionErrors;
  other: string[];
} {
  const fieldErrors: ResolutionErrors = {};
  const other: string[] = [];
  for (const detail of failure.details) {
    const field = toResolutionField(detail.field);
    if (field === null) {
      other.push(detail.message);
    } else {
      const existing = fieldErrors[field];
      fieldErrors[field] = existing === undefined ? detail.message : `${existing} ${detail.message}`;
    }
  }
  return { fieldErrors, other };
}

export type SaveFailureKind =
  | "timeout"
  | "network"
  | "submission_conflict"
  | "save_disabled"
  | "store_unavailable"
  | "store_full"
  | "store_permission_denied"
  | "rejected"
  | "server";

/** Which kind of failure an ApiFailure from POST /api/tickets is (ticket_conflict is handled apart). */
export function classifySaveFailure(failure: ApiFailure): SaveFailureKind {
  if (failure.kind === "network") {
    return "network";
  }
  if (failure.status === 409 && failure.code === "submission_conflict") {
    return "submission_conflict";
  }
  if (failure.status === 403) {
    return "save_disabled";
  }
  if (failure.status === 503) {
    if (failure.code === "store_full") {
      return "store_full";
    }
    if (failure.code === "database_permission_denied") {
      return "store_permission_denied";
    }
    return "store_unavailable";
  }
  return failure.kind === "rejected" ? "rejected" : "server";
}

export const SAVE_FAILURE_TITLES: Record<SaveFailureKind, string> = {
  timeout: "The save timed out",
  network: "The ticket service could not be reached",
  submission_conflict: "This form was already saved with different content",
  save_disabled: "Saving resolved tickets is switched off",
  store_unavailable: "The ticket store is unavailable right now",
  store_full: "The ticket store is full",
  store_permission_denied: "The ticket store does not allow this server to save",
  rejected: "The resolved ticket was not accepted as entered",
  server: "The ticket service had a problem",
};

export const SAVE_FAILURE_ADVICE: Record<SaveFailureKind, string> = {
  timeout:
    "No answer arrived, so it is not known whether the ticket was saved. Press Save resolved ticket again: the same submission is sent, so it cannot create a second record.",
  network:
    "It is not known whether the ticket was saved. Check your connection, then press Save resolved ticket again: the same submission is sent, so it cannot create a second record.",
  submission_conflict:
    "Nothing new was saved. This form was saved earlier with other content, perhaps before an answer was lost. Check the stored ticket before saving again.",
  save_disabled:
    "The ticket was not saved. Send the resolution to the escalation address instead.",
  store_unavailable: "The ticket was not saved. Wait a moment, then press Save resolved ticket again.",
  store_full:
    "The ticket was not saved because the store has no room. Send the resolution to the escalation address instead.",
  store_permission_denied:
    "The ticket was not saved. The server is not permitted to write to its database, which saving again will not fix until an administrator corrects it. Send the resolution to the escalation address instead and quote the request id.",
  rejected: "The ticket was not saved. Correct the form, then press Save resolved ticket again.",
  server:
    "The ticket may not have been saved. Wait a moment, then press Save resolved ticket again: the same submission is sent, so it cannot create a second record.",
};

export interface RecordedField {
  key: string;
  label: string;
  value: string;
}

const RECORDED_INVESTIGATION: ReadonlyArray<[keyof Ticket, string]> = [
  ["investigation", "Investigation approach"],
  ["root_cause", "Root cause"],
  ["verification", "Verification"],
  ["resolved_by", "Resolved by"],
  ["resolved_at", "Resolved at (UTC)"],
];

const RECORDED_PROBLEM: ReadonlyArray<[keyof Ticket, string]> = [
  ["product", "Product or version"],
  ["error_detail", "Error details"],
];

function presentFields(
  ticket: Ticket,
  fields: ReadonlyArray<[keyof Ticket, string]>,
): RecordedField[] {
  const present: RecordedField[] = [];
  for (const [key, label] of fields) {
    const value = ticket[key];
    if (typeof value === "string" && value.trim() !== "") {
      present.push({ key, label, value });
    }
  }
  return present;
}

/** Investigation details recorded through the resolution form, only those present. */
export function recordedInvestigation(ticket: Ticket): RecordedField[] {
  return presentFields(ticket, RECORDED_INVESTIGATION);
}

/** Product and error details recorded through the resolution form, only those present. */
export function recordedProblemDetails(ticket: Ticket): RecordedField[] {
  return presentFields(ticket, RECORDED_PROBLEM);
}
