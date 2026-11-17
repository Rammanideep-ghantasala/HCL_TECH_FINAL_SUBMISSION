import { useEffect, useRef, useState } from "react";
import { ApiFailure, summarizeResults } from "../api/client";
import type { SearchResponse, SummarizationInfo, SummaryResponse } from "../api/types";
import "../styles/summary.css";

export const SUMMARY_TIMEOUT_MS = 60_000;

type SummaryState =
  | { phase: "idle" | "loading" }
  | { phase: "failed"; message: string; refreshRequired: boolean }
  | { phase: "done"; response: SummaryResponse };

/** This component is unmounted on every edit, new search, clear or result change. */
export function SummaryPanel({ response, info, topK }: {
  response: SearchResponse;
  info: SummarizationInfo | undefined;
  topK: 3 | 5;
}) {
  const [state, setState] = useState<SummaryState>({ phase: "idle" });
  const pending = useRef<AbortController | null>(null);
  const count = response.results.length;
  const token = response.summary_token;

  useEffect(() => () => {
    pending.current?.abort();
    pending.current = null;
  }, []);

  const run = async () => {
    if (pending.current || !token || info?.configured === false) return;
    const controller = new AbortController();
    pending.current = controller;
    setState({ phase: "loading" });
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
      if (pending.current === controller) {
        pending.current = null;
        setState({ phase: "failed", refreshRequired: false,
          message: "Summarization timed out. Your search results are still available; you can retry." });
      }
    }, SUMMARY_TIMEOUT_MS);
    try {
      const summary = await summarizeResults({
        summary_token: token,
        ticket_ids: response.results.map((card) => card.ticket.ticket_id),
      }, controller.signal);
      if (pending.current === controller && !controller.signal.aborted) {
        setState({ phase: "done", response: summary });
      }
    } catch (error) {
      if (pending.current !== controller || (controller.signal.aborted && !timedOut)) return;
      setState({
        phase: "failed",
        message: timedOut ? "Summarization timed out. Your search results are still available; you can retry."
          : error instanceof ApiFailure && error.message ? error.message
            : "The summary could not be loaded. Your search results are still available; please retry.",
        refreshRequired: error instanceof ApiFailure && error.code === "summary_refresh_required",
      });
    } finally {
      window.clearTimeout(timer);
      if (pending.current === controller) pending.current = null;
    }
  };

  const needsRefresh = !token || (state.phase === "failed" && state.refreshRequired);
  return (
    <section className="summary-panel" aria-labelledby="summary-heading" aria-busy={state.phase === "loading"}>
      <h3 id="summary-heading" className="section-heading">Summarize these matches</h3>
      <p className="basis">
        {count < topK ? `Only ${count} matching ${count === 1 ? "ticket qualifies" : "tickets qualify"}. ` : ""}
        {count} of up to {topK} matches shown. The summary covers this main result list in order.
      </p>
      <p className="basis">{info?.data_notice ?? "Clicking Summarize sends this query and these match tickets to OpenAI. Related context is excluded."}</p>
      {info?.configured === false ? <p className="inline-warning">Summaries are not configured on this server. Similarity search is still available.</p> : null}
      {!token ? <p className="inline-warning">Search again to get a current summary reference.</p> : null}
      <button className="button" type="button" onClick={() => { void run(); }}
        disabled={state.phase === "loading" || needsRefresh || info?.configured === false}>
        {state.phase === "loading" ? "Summarizing…"
          : state.phase === "failed" ? "Retry summary"
            : `Summarize ${count} ${count === 1 ? "ticket" : "tickets"}`}
      </button>
      {state.phase === "loading" ? <p role="status">Preparing a summary of {count} displayed tickets…</p> : null}
      {state.phase === "failed" ? <p className="field-error" role="alert">{state.message}</p> : null}
      {state.phase === "done" ? (
        <div className="summary-content">
          <h4>Summary of {count} displayed {count === 1 ? "ticket" : "tickets"}</h4>
          <p className="basis">{state.response.disclaimer}</p>
          <ul className="summary-overview">
            {state.response.summary.overview.map((point, index) => (
              <li key={index}>{point.text}{" "}{point.ticket_ids.map((id) => (
                <a className="summary-source" key={id} href={`#ticket-result-${id}`}>{id}</a>
              ))}</li>
            ))}
          </ul>
          <ol className="summary-tickets">
            {state.response.summary.tickets.map((ticket) => (
              <li key={ticket.ticket_id}>
                <h5><a href={`#ticket-result-${ticket.ticket_id}`}>{ticket.ticket_id} — original ticket</a></h5>
                <p><strong>Reported issue:</strong> {ticket.reported_issue}</p>
                <p><strong>Recorded resolution or workaround:</strong> {ticket.recorded_resolution}</p>
                {ticket.applicability_notes.length > 0 ? <ul>
                  {ticket.applicability_notes.map((note, index) => <li key={index}>{note}</li>)}
                </ul> : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </section>
  );
}
