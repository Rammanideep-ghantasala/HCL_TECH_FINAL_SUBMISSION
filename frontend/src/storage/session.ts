/**
 * Session-only demo data: recent searches and feedback verdicts.
 * Everything lives in sessionStorage for this browser tab. Every access is
 * guarded, so the app keeps working when storage is blocked or full.
 */
import type { MatchLevel, RetrievalMode, SearchStatus } from "../api/types";
import { FEEDBACK_LIMIT, MAX_STORED_CHARS, RECENT_LIMIT } from "../constants";
import { FIELD_NAMES, type FormValues } from "../state/search";

export const RECENT_KEY = "ticket-similarity.recent-searches.v1";
export const FEEDBACK_KEY = "ticket-similarity.feedback.v1";

/** One remembered search: the inputs and how the search ended. Never results. */
export interface RecentSearch extends FormValues {
  /** ISO time the search finished. */
  time: string;
  /** A SearchStatus, or "failed" when the search did not complete. */
  outcome: SearchStatus | "failed";
}

export type Verdict = "useful" | "not_useful";

export const REASON_OPTIONS = [
  { value: "wrong_product", label: "Wrong product" },
  { value: "different_symptom", label: "Different symptom" },
  { value: "outdated_fix", label: "Outdated fix" },
  { value: "cant_tell_missing_information", label: "Can't tell — missing information" },
  { value: "other", label: "Other" },
] as const;

export type FeedbackReason = (typeof REASON_OPTIONS)[number]["value"];

export function isFeedbackReason(value: string): value is FeedbackReason {
  return REASON_OPTIONS.some((option) => option.value === value);
}

/** One verdict on one result card. There is at most one record per result_id. */
export interface FeedbackRecord {
  event_id: string;
  time: string;
  request_id: string;
  query_fingerprint: string;
  result_id: string;
  ticket_id: string;
  rank: number;
  match_level: MatchLevel;
  verdict: Verdict;
  reason: FeedbackReason | null;
  /** Ticket ids shown for this search, in display order. */
  results_shown: string[];
  status: SearchStatus;
  mode: RetrievalMode;
  degraded: boolean;
  index_version: string;
  relevance_config_version: string;
  model_id: string | null;
  /** The inputs that were searched. Left out of exports unless asked for. */
  searched_input: FormValues;
}

function getStorage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function readList(key: string): unknown[] {
  try {
    const raw = getStorage()?.getItem(key);
    if (!raw || raw.length > MAX_STORED_CHARS) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * Writes a list, dropping its oldest items until it fits under the size cap.
 * `oldestFirst` says which end of the list is the oldest.
 */
function writeList(key: string, items: readonly unknown[], oldestFirst: boolean): void {
  try {
    const storage = getStorage();
    if (!storage) {
      return;
    }
    let kept = [...items];
    let text = JSON.stringify(kept);
    while (text.length > MAX_STORED_CHARS && kept.length > 0) {
      kept = oldestFirst ? kept.slice(1) : kept.slice(0, -1);
      text = JSON.stringify(kept);
    }
    if (kept.length === 0) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, text);
    }
  } catch {
    // Storage unavailable or full: the in-memory copy still works for this page.
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasFormValues(value: Record<string, unknown>): boolean {
  return (value.top_k === undefined || value.top_k === 3 || value.top_k === 5)
    && FIELD_NAMES.every((name) => typeof value[name] === "string");
}

function isRecentSearch(value: unknown): value is RecentSearch {
  return (
    isRecord(value) &&
    hasFormValues(value) &&
    typeof value.time === "string" &&
    typeof value.outcome === "string"
  );
}

function isFeedbackRecord(value: unknown): value is FeedbackRecord {
  return (
    isRecord(value) &&
    typeof value.event_id === "string" &&
    typeof value.time === "string" &&
    typeof value.request_id === "string" &&
    typeof value.result_id === "string" &&
    typeof value.ticket_id === "string" &&
    (value.verdict === "useful" || value.verdict === "not_useful") &&
    Array.isArray(value.results_shown) &&
    isRecord(value.searched_input) &&
    hasFormValues(value.searched_input)
  );
}

export function loadRecent(): RecentSearch[] {
  return readList(RECENT_KEY).filter(isRecentSearch).slice(0, RECENT_LIMIT);
}

/** `items` are newest first. */
export function saveRecent(items: readonly RecentSearch[]): void {
  writeList(RECENT_KEY, items.slice(0, RECENT_LIMIT), false);
}

export function loadFeedback(): FeedbackRecord[] {
  return readList(FEEDBACK_KEY).filter(isFeedbackRecord).slice(-FEEDBACK_LIMIT);
}

/** `items` are oldest first. */
export function saveFeedback(items: readonly FeedbackRecord[]): void {
  writeList(FEEDBACK_KEY, items.slice(-FEEDBACK_LIMIT), true);
}

/** Puts `entry` first, removes an earlier entry with the same inputs, keeps the newest few. */
export function addRecent(list: readonly RecentSearch[], entry: RecentSearch): RecentSearch[] {
  const others = list.filter((item) => !FIELD_NAMES.every((name) => item[name] === entry[name]));
  return [entry, ...others].slice(0, RECENT_LIMIT);
}

/** Replaces any earlier record for the same result_id. */
export function upsertFeedback(
  list: readonly FeedbackRecord[],
  record: FeedbackRecord,
): FeedbackRecord[] {
  return [...list.filter((item) => item.result_id !== record.result_id), record].slice(
    -FEEDBACK_LIMIT,
  );
}

export function removeFeedback(list: readonly FeedbackRecord[], resultId: string): FeedbackRecord[] {
  return list.filter((item) => item.result_id !== resultId);
}

/** A random id for a feedback event. Works without a secure context. */
export function newEventId(): string {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The JSON text of a feedback export. Ticket text is included only on request. */
export function buildFeedbackExport(
  records: readonly FeedbackRecord[],
  includeSearchedText: boolean,
  exportedAt: string,
): string {
  const exported = records.map((record) => {
    const { searched_input: searchedInput, ...rest } = record;
    return includeSearchedText ? { ...rest, searched_input: searchedInput } : rest;
  });
  return JSON.stringify(
    {
      exported_at: exportedAt,
      scope: "Feedback recorded in one browser tab. It was never sent to the server.",
      includes_searched_text: includeSearchedText,
      record_count: exported.length,
      records: exported,
    },
    null,
    2,
  );
}

/** Offers `text` as a file download using a temporary link. */
export function downloadJson(filename: string, text: string): void {
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.className = "visually-hidden";
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Released a little later so the browser has started the download first.
  window.setTimeout(() => {
    try {
      URL.revokeObjectURL(url);
    } catch {
      // Nothing to release.
    }
  }, 1000);
}
