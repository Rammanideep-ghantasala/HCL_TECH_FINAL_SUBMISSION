import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiFailure, fetchTicket, type FetchedTicket } from "../api/client";

export type TicketEntry =
  | { status: "loading" }
  | { status: "loaded"; ticket: FetchedTicket }
  | { status: "error"; message: string };

interface TicketCache {
  entries: Readonly<Record<string, TicketEntry>>;
  load: (ticketId: string) => void;
  /** Forgets one ticket so that the next view of it fetches the stored version again. */
  invalidate: (ticketId: string) => void;
}

const TicketCacheContext = createContext<TicketCache | null>(null);

function describeFailure(ticketId: string, error: unknown): string {
  if (error instanceof ApiFailure && error.status === 404) {
    return `Ticket ${ticketId} was not found in the stored history.`;
  }
  if (error instanceof ApiFailure && error.kind === "network") {
    return `Ticket ${ticketId} could not be loaded because the service could not be reached.`;
  }
  return `Ticket ${ticketId} could not be loaded.`;
}

/**
 * Stored tickets fetched by id. Each id is requested at most once until it is
 * invalidated (after this page saved that ticket, or re-indexed it); failed
 * lookups can be tried again.
 */
export function TicketCacheProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<Record<string, TicketEntry>>({});
  /** Ids that are loading or loaded; guards against duplicate requests. */
  const claimedRef = useRef(new Set<string>());
  /** Bumped on invalidation so that an answer to an older request is ignored. */
  const generationRef = useRef(new Map<string, number>());

  const load = useCallback((ticketId: string) => {
    if (claimedRef.current.has(ticketId)) {
      return;
    }
    claimedRef.current.add(ticketId);
    const generation = generationRef.current.get(ticketId) ?? 0;
    const isCurrent = () => (generationRef.current.get(ticketId) ?? 0) === generation;
    setEntries((previous) => ({ ...previous, [ticketId]: { status: "loading" } }));
    fetchTicket(ticketId).then(
      (ticket) => {
        if (!isCurrent()) {
          return;
        }
        setEntries((previous) => ({ ...previous, [ticketId]: { status: "loaded", ticket } }));
      },
      (error: unknown) => {
        if (!isCurrent()) {
          return;
        }
        claimedRef.current.delete(ticketId);
        setEntries((previous) => ({
          ...previous,
          [ticketId]: { status: "error", message: describeFailure(ticketId, error) },
        }));
      },
    );
  }, []);

  const invalidate = useCallback((ticketId: string) => {
    generationRef.current.set(ticketId, (generationRef.current.get(ticketId) ?? 0) + 1);
    claimedRef.current.delete(ticketId);
    setEntries((previous) => {
      if (!(ticketId in previous)) {
        return previous;
      }
      const next = { ...previous };
      delete next[ticketId];
      return next;
    });
  }, []);

  const value = useMemo(() => ({ entries, load, invalidate }), [entries, load, invalidate]);
  return <TicketCacheContext.Provider value={value}>{children}</TicketCacheContext.Provider>;
}

/** Returns the cache entry for one ticket id and a function that requests it. */
export function useTicket(ticketId: string): { entry: TicketEntry | undefined; load: () => void } {
  const cache = useContext(TicketCacheContext);
  if (cache === null) {
    throw new Error("useTicket must be used inside TicketCacheProvider");
  }
  const { entries, load } = cache;
  const request = useCallback(() => load(ticketId), [load, ticketId]);
  return { entry: entries[ticketId], load: request };
}

function ignoreInvalidation(): void {
  // Outside a TicketCacheProvider there is no cache to update.
}

/**
 * Returns a function that drops one ticket from the cache. Views of that
 * ticket that are open fetch it again; closed ones fetch it when next opened.
 */
export function useInvalidateTicket(): (ticketId: string) => void {
  const cache = useContext(TicketCacheContext);
  return cache === null ? ignoreInvalidation : cache.invalidate;
}
