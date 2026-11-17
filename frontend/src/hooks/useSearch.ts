import { useCallback, useRef, useState } from "react";
import { ApiFailure, isAbortError, searchTickets } from "../api/client";
import type { SearchResponse } from "../api/types";
import { SEARCH_TIMEOUT_MS } from "../constants";
import {
  defaultFailureMessage,
  isKnownStatus,
  mapRejection,
  toPayload,
  type FailureKind,
  type FormValues,
  type SearchState,
} from "../state/search";

/** What a search settled as. `null` from runSearch means "ignored or superseded". */
export type SearchOutcome =
  | { type: "done"; submitted: FormValues; response: SearchResponse }
  | {
      type: "failed";
      submitted: FormValues;
      kind: FailureKind;
      message: string;
      requestId: string | null;
      /** Present when the server answered with an error body. */
      failure: ApiFailure | null;
    };

function failed(
  submitted: FormValues,
  kind: FailureKind,
  failure: ApiFailure | null,
  message?: string,
): SearchOutcome {
  const serverMessage = failure?.message ?? "";
  // Server details that do not belong to a form field are shown with the message.
  const unplaced = failure && kind === "rejected" ? mapRejection(failure).other : [];
  const base = message ?? (serverMessage !== "" ? serverMessage : defaultFailureMessage(kind));
  return {
    type: "failed",
    submitted,
    kind,
    message: [base, ...unplaced].join(" "),
    requestId: failure?.requestId ?? null,
    failure,
  };
}

export interface UseSearch {
  state: SearchState;
  /**
   * Sends one search. Call it from event handlers only. Resolves with the
   * outcome that was committed to the screen, or null when the call was
   * ignored (identical request already in flight) or superseded.
   */
  runSearch: (submitted: FormValues) => Promise<SearchOutcome | null>;
  /** Aborts any request and returns to the idle state. */
  reset: () => void;
}

export function useSearch(): UseSearch {
  const [state, setState] = useState<SearchState>({ phase: "idle" });
  /** Incremented for every search and every reset. Only the latest may commit. */
  const sequenceRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  /** Serialised payload of the request currently in flight, if any. */
  const inFlightRef = useRef<string | null>(null);

  const runSearch = useCallback(async (submitted: FormValues): Promise<SearchOutcome | null> => {
    const payload = toPayload(submitted);
    const payloadKey = JSON.stringify(payload);
    if (inFlightRef.current === payloadKey) {
      return null;
    }

    controllerRef.current?.abort();
    sequenceRef.current += 1;
    const sequence = sequenceRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    inFlightRef.current = payloadKey;

    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, SEARCH_TIMEOUT_MS);

    setState({ phase: "loading", submitted });

    let outcome: SearchOutcome;
    try {
      const response = await searchTickets(payload, controller.signal);
      outcome = isKnownStatus(response.status)
        ? { type: "done", submitted, response }
        : failed(
            submitted,
            "server",
            null,
            "The server sent an outcome this page does not recognise, so no results are shown.",
          );
    } catch (error) {
      if (isAbortError(error)) {
        if (!timedOut) {
          // Superseded by a newer search or by Clear: say nothing.
          return null;
        }
        outcome = failed(submitted, "timeout", null);
      } else if (error instanceof ApiFailure) {
        outcome = failed(submitted, error.kind, error);
      } else {
        outcome = failed(submitted, "server", null);
      }
    } finally {
      window.clearTimeout(timer);
    }

    if (sequence !== sequenceRef.current) {
      return null;
    }
    controllerRef.current = null;
    inFlightRef.current = null;
    if (outcome.type === "done") {
      setState({ phase: "done", submitted, response: outcome.response });
    } else {
      setState({
        phase: "failed",
        submitted,
        kind: outcome.kind,
        message: outcome.message,
        requestId: outcome.requestId,
      });
    }
    return outcome;
  }, []);

  const reset = useCallback(() => {
    sequenceRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    inFlightRef.current = null;
    setState({ phase: "idle" });
  }, []);

  return { state, runSearch, reset };
}
