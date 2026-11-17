import { useId } from "react";
import { FEEDBACK_NOTE } from "../constants";
import {
  isFeedbackReason,
  REASON_OPTIONS,
  type FeedbackReason,
  type Verdict,
} from "../storage/session";

interface FeedbackControlsProps {
  ticketId: string;
  verdict: Verdict | null;
  reason: FeedbackReason | null;
  /** Called with the new verdict (null withdraws it) and the reason that goes with it. */
  onChange: (verdict: Verdict | null, reason: FeedbackReason | null) => void;
}

/** "Useful" / "Not useful" for one result. Kept in this browser tab only. */
export function FeedbackControls({ ticketId, verdict, reason, onChange }: FeedbackControlsProps) {
  const reasonId = useId();

  return (
    <div className="feedback" role="group" aria-label={`Feedback on ${ticketId}`}>
      <span className="feedback-question">Was {ticketId} worth considering?</span>
      <div className="feedback-buttons">
        <button
          type="button"
          className="button button-toggle"
          aria-pressed={verdict === "useful"}
          onClick={() => onChange(verdict === "useful" ? null : "useful", null)}
        >
          Useful
        </button>
        <button
          type="button"
          className="button button-toggle"
          aria-pressed={verdict === "not_useful"}
          onClick={() => onChange(verdict === "not_useful" ? null : "not_useful", null)}
        >
          Not useful
        </button>
      </div>
      {verdict === "not_useful" ? (
        <div className="feedback-reason">
          <label htmlFor={reasonId}>Reason (optional)</label>
          <select
            id={reasonId}
            value={reason ?? ""}
            onChange={(event) => {
              const value = event.target.value;
              onChange("not_useful", isFeedbackReason(value) ? value : null);
            }}
          >
            <option value="">No reason given</option>
            {REASON_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      <p className="basis feedback-note">{FEEDBACK_NOTE}</p>
    </div>
  );
}
