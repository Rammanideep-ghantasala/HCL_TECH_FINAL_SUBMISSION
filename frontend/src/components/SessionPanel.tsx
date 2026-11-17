import { useId, useState } from "react";
import { SESSION_EXPLANATION } from "../constants";
import { summariseInput } from "../state/search";
import type { RecentSearch } from "../storage/session";

const OUTCOME_LABELS: Record<string, string> = {
  useful_matches: "Useful matches",
  no_strong_match: "No strong match",
  insufficient_input: "More detail needed",
  failed: "Did not complete",
};

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "time not recorded";
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export interface SessionPanelProps {
  recent: RecentSearch[];
  feedbackCount: number;
  onLoadRecent: (entry: RecentSearch) => void;
  onExport: (includeSearchedText: boolean) => void;
  onClear: () => void;
}

/** Recent searches and feedback that live only in this browser tab. */
export function SessionPanel({
  recent,
  feedbackCount,
  onLoadRecent,
  onExport,
  onClear,
}: SessionPanelProps) {
  const [includeText, setIncludeText] = useState(false);
  const includeId = useId();
  const nothingStored = recent.length === 0 && feedbackCount === 0;

  return (
    <section className="panel session" aria-labelledby="session-heading">
      <h2 id="session-heading" className="panel-heading">
        This browser tab only
      </h2>
      <p className="hint">{SESSION_EXPLANATION}</p>

      <h3 className="section-heading">Recent in this tab</h3>
      {recent.length === 0 ? (
        <p className="muted">No searches yet in this tab.</p>
      ) : (
        <ul className="recent-list">
          {recent.map((entry, index) => (
            <li key={`${entry.time}-${index}`}>
              <button type="button" className="recent-button" onClick={() => onLoadRecent(entry)}>
                <span className="recent-summary">{summariseInput(entry)}</span>
                <span className="recent-meta">
                  {formatTime(entry.time)} · {OUTCOME_LABELS[entry.outcome] ?? "Outcome not recorded"}
                  {entry.category !== "" ? ` · Filter: ${entry.category}` : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {recent.length > 0 ? (
        <p className="basis">Choosing one fills the form. It does not search.</p>
      ) : null}

      <h3 className="section-heading">Feedback in this tab</h3>
      <p className="muted">
        {feedbackCount === 0
          ? "No feedback recorded yet."
          : feedbackCount === 1
            ? "1 feedback record."
            : `${feedbackCount} feedback records.`}
      </p>
      <div className="checkbox-row">
        <input
          id={includeId}
          type="checkbox"
          checked={includeText}
          onChange={(event) => setIncludeText(event.target.checked)}
        />
        <label htmlFor={includeId}>Include the searched ticket text in the export</label>
      </div>
      <div className="session-actions">
        <button
          type="button"
          className="button button-small"
          disabled={feedbackCount === 0}
          onClick={() => onExport(includeText)}
        >
          Export feedback (JSON)
        </button>
        <button
          type="button"
          className="button button-small"
          disabled={nothingStored}
          onClick={onClear}
        >
          Clear session data
        </button>
      </div>
    </section>
  );
}
