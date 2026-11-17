/**
 * Thin fetch wrapper for the same-origin API. All URLs are relative.
 * Nothing here logs, and ticket text never goes into a URL.
 */
import type {
  ErrorDetail,
  ExampleTicket,
  IndexingStatus,
  MetaResponse,
  RefreshIndexResponse,
  SaveTicketRequest,
  SaveTicketResponse,
  SearchRequest,
  SearchResponse,
  SummaryRequest,
  SummaryResponse,
  StoredTicket,
  Ticket,
} from "./types";

/** Why a request did not produce a usable answer (timeouts are decided by the caller). */
export type ApiFailureKind = "network" | "server" | "rejected";

interface ApiFailureInit {
  kind: ApiFailureKind;
  status: number | null;
  code: string | null;
  message: string;
  requestId: string | null;
  details: ErrorDetail[];
}

export class ApiFailure extends Error {
  readonly kind: ApiFailureKind;
  readonly status: number | null;
  readonly code: string | null;
  readonly requestId: string | null;
  readonly details: ErrorDetail[];

  constructor(init: ApiFailureInit) {
    super(init.message);
    this.name = "ApiFailure";
    this.kind = init.kind;
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.details = init.details;
  }
}

/** Statuses that mean "the server understood the request and refused this input". */
const REJECTED_STATUSES = new Set([400, 413, 422]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export function isAbortError(error: unknown): boolean {
  return isRecord(error) && error.name === "AbortError";
}

function readErrorBody(body: unknown): {
  code: string | null;
  message: string;
  requestId: string | null;
  details: ErrorDetail[];
} {
  const error = isRecord(body) && isRecord(body.error) ? body.error : null;
  if (!error) {
    return { code: null, message: "", requestId: null, details: [] };
  }
  const details: ErrorDetail[] = [];
  if (Array.isArray(error.details)) {
    for (const item of error.details) {
      if (isRecord(item) && typeof item.message === "string") {
        details.push({
          field: typeof item.field === "string" ? item.field : null,
          message: item.message,
        });
      }
    }
  }
  return {
    code: typeof error.code === "string" ? error.code : null,
    message: typeof error.message === "string" ? error.message : "",
    requestId: typeof error.request_id === "string" ? error.request_id : null,
    details,
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    return undefined;
  }
}

function unexpectedBody(status: number): ApiFailure {
  return new ApiFailure({
    kind: "server",
    status,
    code: null,
    message: "The server sent an answer this page could not read.",
    requestId: null,
    details: [],
  });
}

interface RequestOptions {
  method: "GET" | "POST";
  jsonBody?: unknown;
  signal?: AbortSignal;
}

async function requestJson(path: string, options: RequestOptions): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method: options.method, headers, signal: options.signal };
  if (options.jsonBody !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.jsonBody);
  }

  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ApiFailure({
      kind: "network",
      status: null,
      code: null,
      message: "",
      requestId: null,
      details: [],
    });
  }

  const body = await readJson(response);

  if (!response.ok) {
    const parsed = readErrorBody(body);
    throw new ApiFailure({
      kind: REJECTED_STATUSES.has(response.status) ? "rejected" : "server",
      status: response.status,
      code: parsed.code,
      message: parsed.message,
      requestId: parsed.requestId,
      details: parsed.details,
    });
  }
  if (body === undefined) {
    throw unexpectedBody(response.status);
  }
  return body;
}

function looksLikeCard(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.result_id === "string" &&
    isRecord(value.ticket) &&
    typeof value.ticket.ticket_id === "string" &&
    isRecord(value.evidence) &&
    isRecord(value.differences) &&
    isRecord(value.scores) &&
    Array.isArray(value.verification_questions) &&
    Array.isArray(value.referenced_ticket_ids)
  );
}

function looksLikeSearchResponse(body: unknown): body is SearchResponse {
  return (
    isRecord(body) &&
    typeof body.status === "string" &&
    typeof body.message === "string" &&
    typeof body.request_id === "string" &&
    Array.isArray(body.results) &&
    body.results.every(looksLikeCard) &&
    Array.isArray(body.related) &&
    body.related.every(looksLikeCard) &&
    isRecord(body.query_insights) &&
    Array.isArray(body.query_insights.suggestions) &&
    Array.isArray(body.query_insights.unseen_terms) &&
    isRecord(body.retrieval)
  );
}

function looksLikeTicket(body: unknown): body is Ticket {
  return (
    isRecord(body) &&
    typeof body.ticket_id === "string" &&
    typeof body.subject === "string" &&
    typeof body.description === "string" &&
    typeof body.resolution_notes === "string" &&
    Array.isArray(body.tags)
  );
}

function looksLikeStoredTicket(body: unknown): body is StoredTicket {
  return isRecord(body) && typeof body.revision === "string" && looksLikeTicket(body);
}

function looksLikeIndexing(value: unknown): value is IndexingStatus {
  return (
    isRecord(value) &&
    (value.status === "indexed" || value.status === "failed") &&
    typeof value.message === "string"
  );
}

function looksLikeSaveResponse(body: unknown): body is SaveTicketResponse {
  return (
    isRecord(body) &&
    typeof body.outcome === "string" &&
    typeof body.message === "string" &&
    looksLikeStoredTicket(body.ticket) &&
    looksLikeIndexing(body.indexing)
  );
}

function looksLikeRefreshResponse(body: unknown): body is RefreshIndexResponse {
  return (
    isRecord(body) &&
    looksLikeIndexing(body.indexing) &&
    isStringArray(body.unindexed_ticket_ids) &&
    (body.rejected_ticket_ids === undefined || isStringArray(body.rejected_ticket_ids))
  );
}

function looksLikeMeta(body: unknown): body is MetaResponse {
  if (!isRecord(body) || !isRecord(body.limits) || !isRecord(body.retrieval)) {
    return false;
  }
  const limits = body.limits;
  const limitKeys = [
    "subject_max_chars",
    "description_max_chars",
    "product_max_chars",
    "error_detail_max_chars",
  ];
  return (
    typeof body.data_notice === "string" &&
    isStringArray(body.categories) &&
    limitKeys.every((key) => typeof limits[key] === "number" && limits[key] > 0)
  );
}

function looksLikeExample(value: unknown): value is ExampleTicket {
  return (
    isRecord(value) &&
    typeof value.ticket_id === "string" &&
    typeof value.subject === "string" &&
    typeof value.description === "string"
  );
}

export async function searchTickets(
  payload: SearchRequest,
  signal: AbortSignal,
): Promise<SearchResponse> {
  const body = await requestJson("/api/search", {
    method: "POST",
    jsonBody: payload,
    signal,
  });
  if (!looksLikeSearchResponse(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

/** Validate source coverage at the UI boundary as well as on the server. */
export async function summarizeResults(payload: SummaryRequest, signal: AbortSignal): Promise<SummaryResponse> {
  const body = await requestJson("/api/summaries", { method: "POST", jsonBody: payload, signal });
  if (!isRecord(body) || !isStringArray(body.ticket_ids)
    || JSON.stringify(body.ticket_ids) !== JSON.stringify(payload.ticket_ids)
    || !isStringArray(body.source_revisions) || body.source_revisions.length !== payload.ticket_ids.length
    || typeof body.model !== "string" || typeof body.prompt_version !== "string"
    || typeof body.generated_at !== "string" || typeof body.disclaimer !== "string"
    || !isRecord(body.summary) || !Array.isArray(body.summary.tickets)
    || !Array.isArray(body.summary.overview) || body.summary.overview.length === 0) {
    throw unexpectedBody(200);
  }
  const tickets = body.summary.tickets;
  if (tickets.length !== payload.ticket_ids.length || !tickets.every((ticket: unknown, index: number) =>
    isRecord(ticket) && ticket.ticket_id === payload.ticket_ids[index]
    && typeof ticket.reported_issue === "string" && typeof ticket.recorded_resolution === "string"
    && isStringArray(ticket.applicability_notes))
    || !body.summary.overview.every((point: unknown) => isRecord(point)
      && typeof point.text === "string" && isStringArray(point.ticket_ids) && point.ticket_ids.length > 0
      && new Set(point.ticket_ids).size === point.ticket_ids.length
      && point.ticket_ids.every((id) => payload.ticket_ids.includes(id)))) {
    throw unexpectedBody(200);
  }
  return body as unknown as SummaryResponse;
}

/**
 * A ticket as GET /api/tickets/{id} returns it: the store's current version.
 * The save-state fields are optional so that a server that leaves them out
 * still works; only `indexed === false` is treated as "not in search results".
 */
export type FetchedTicket = Ticket &
  Partial<Pick<StoredTicket, "revision" | "indexed" | "index_status">>;

export async function fetchTicket(ticketId: string): Promise<FetchedTicket> {
  const body = await requestJson(`/api/tickets/${encodeURIComponent(ticketId)}`, {
    method: "GET",
  });
  if (!looksLikeTicket(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

/** The stored ticket with its current revision, fetched fresh (never from the page cache). */
export async function fetchStoredTicket(ticketId: string, signal?: AbortSignal): Promise<StoredTicket> {
  const body = await requestJson(`/api/tickets/${encodeURIComponent(ticketId)}`, {
    method: "GET",
    signal,
  });
  if (!looksLikeStoredTicket(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

/**
 * Saves a resolved ticket. A 409 is an ApiFailure of kind "server"; use its
 * status and code ("ticket_conflict", "submission_conflict") to tell them apart.
 */
export async function saveResolvedTicket(
  payload: SaveTicketRequest,
  signal?: AbortSignal,
): Promise<SaveTicketResponse> {
  const body = await requestJson("/api/tickets", { method: "POST", jsonBody: payload, signal });
  if (!looksLikeSaveResponse(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

/** Asks the server to bring the search index up to date with the saved tickets. */
export async function refreshIndex(signal?: AbortSignal): Promise<RefreshIndexResponse> {
  const body = await requestJson("/api/index/refresh", { method: "POST", signal });
  if (!looksLikeRefreshResponse(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

export async function fetchMeta(signal?: AbortSignal): Promise<MetaResponse> {
  const body = await requestJson("/api/meta", { method: "GET", signal });
  if (!looksLikeMeta(body)) {
    throw unexpectedBody(200);
  }
  return body;
}

export async function fetchExamples(signal?: AbortSignal): Promise<ExampleTicket[]> {
  const body = await requestJson("/api/examples", { method: "GET", signal });
  if (!isRecord(body) || !Array.isArray(body.examples)) {
    throw unexpectedBody(200);
  }
  return body.examples.filter(looksLikeExample);
}
