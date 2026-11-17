import { act, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { App } from "../App";
import type { MetaResponse, SaveTicketRequest, SearchRequest, SummaryRequest, Ticket } from "../api/types";
import { examplesFixture, makeApiError, metaFixture } from "./fixtures";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A request that waits until the test answers it. */
export interface PendingCall<TBody> {
  body: TBody;
  signal: AbortSignal | null;
  /** Delivers the server's answer for this call. */
  respond: (status: number, body: unknown) => void;
  /** Fails the call as if the network were down. */
  fail: () => void;
}

export type SearchCall = PendingCall<SearchRequest>;

function pendingCall<TBody>(
  calls: Array<PendingCall<TBody>>,
  body: TBody,
  signal: AbortSignal | null,
  rejectOnAbort: boolean,
): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    if (rejectOnAbort) {
      signal?.addEventListener("abort", () => {
        reject(new DOMException("The operation was aborted.", "AbortError"));
      });
    }
    calls.push({
      body,
      signal,
      respond: (status, answer) => resolve(jsonResponse(status, answer)),
      fail: () => reject(new TypeError("Failed to fetch")),
    });
  });
}

interface FakeApiOptions {
  /** Body for GET /api/meta (defaults to metaFixture). */
  meta?: MetaResponse;
  /** Status for GET /api/meta. */
  metaStatus?: number;
  /** Status for GET /api/examples. */
  examplesStatus?: number;
  /** Stored tickets served by GET /api/tickets/{id}. Pass StoredTicket values to include revisions. */
  tickets?: Ticket[];
  /**
   * When false, an aborted search stays pending so a test can deliver its
   * answer late and prove that the page ignores it.
   */
  rejectOnAbort?: boolean;
  /** Answers every search immediately instead of waiting for respond(). */
  autoRespond?: (body: SearchRequest) => { status: number; body: unknown };
}

export interface FakeApi {
  searchCalls: SearchCall[];
  summaryCalls: Array<PendingCall<SummaryRequest>>;
  /** POST /api/tickets, each waiting for an answer. */
  saveCalls: Array<PendingCall<SaveTicketRequest>>;
  /** POST /api/index/refresh, each waiting for an answer. `body` is the raw body, null when none. */
  refreshCalls: Array<PendingCall<string | null>>;
  ticketRequests: string[];
  fetchMock: ReturnType<typeof vi.fn>;
}

/** Replaces fetch with an in-memory API. No network is used. */
export function installFakeApi(options: FakeApiOptions = {}): FakeApi {
  const searchCalls: SearchCall[] = [];
  const summaryCalls: Array<PendingCall<SummaryRequest>> = [];
  const saveCalls: Array<PendingCall<SaveTicketRequest>> = [];
  const refreshCalls: Array<PendingCall<string | null>> = [];
  const ticketRequests: string[] = [];
  const rejectOnAbort = options.rejectOnAbort ?? true;

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (url === "/api/meta") {
      const status = options.metaStatus ?? 200;
      return Promise.resolve(
        status === 200
          ? jsonResponse(200, options.meta ?? metaFixture)
          : jsonResponse(status, makeApiError("internal_error", "Meta failed.")),
      );
    }

    if (url === "/api/tickets" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as SaveTicketRequest;
      return pendingCall(saveCalls, body, init?.signal ?? null, true);
    }

    if (url === "/api/summaries" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as SummaryRequest;
      return pendingCall(summaryCalls, body, init?.signal ?? null, rejectOnAbort);
    }

    if (url === "/api/index/refresh" && method === "POST") {
      // The raw body is recorded so a test can check that none was sent.
      const raw = init?.body === undefined || init.body === null ? null : String(init.body);
      return pendingCall(refreshCalls, raw, init?.signal ?? null, true);
    }

    if (url === "/api/examples") {
      const status = options.examplesStatus ?? 200;
      return Promise.resolve(
        status === 200
          ? jsonResponse(200, examplesFixture)
          : jsonResponse(status, makeApiError("internal_error", "Examples failed.")),
      );
    }

    if (url.startsWith("/api/tickets/") && method === "GET") {
      const id = decodeURIComponent(url.slice("/api/tickets/".length));
      ticketRequests.push(id);
      const ticket = options.tickets?.find((item) => item.ticket_id === id);
      return Promise.resolve(
        ticket
          ? jsonResponse(200, ticket)
          : jsonResponse(404, makeApiError("ticket_not_found", "No such ticket.")),
      );
    }

    if (url === "/api/search") {
      const body = JSON.parse(String(init?.body)) as SearchRequest;
      const signal = init?.signal ?? null;
      if (options.autoRespond) {
        const answer = options.autoRespond(body);
        searchCalls.push({ body, signal, respond: () => undefined, fail: () => undefined });
        return Promise.resolve(jsonResponse(answer.status, answer.body));
      }
      return pendingCall(searchCalls, body, signal, rejectOnAbort);
    }

    return Promise.resolve(jsonResponse(404, makeApiError("not_found", "Not found.")));
  });

  vi.stubGlobal("fetch", fetchMock);
  return { searchCalls, summaryCalls, saveCalls, refreshCalls, ticketRequests, fetchMock };
}

/** Renders the app and waits until the settings and examples requests have been handled. */
export async function renderApp(): Promise<ReturnType<typeof render>> {
  const view = render(<App />);
  await screen.findByRole("heading", { name: "Before you search" });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return view;
}

/** Delivers an answer and lets every resulting update finish. */
export async function respond(
  call: PendingCall<unknown> | undefined,
  status: number,
  body: unknown,
) {
  if (!call) {
    throw new Error("No such call was made.");
  }
  await act(async () => {
    call.respond(status, body);
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** Fails a call as a network error and lets every resulting update finish. */
export async function failNetwork(call: PendingCall<unknown> | undefined) {
  if (!call) {
    throw new Error("No such call was made.");
  }
  await act(async () => {
    call.fail();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

export function subjectInput(): HTMLElement {
  return screen.getByLabelText("Subject");
}

export function searchButton(): HTMLElement {
  return screen.getByRole("button", { name: "Search" });
}
