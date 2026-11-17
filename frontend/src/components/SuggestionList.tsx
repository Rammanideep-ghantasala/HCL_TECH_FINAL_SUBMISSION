import type { Suggestion } from "../api/types";
import { FIELD_LABELS, type FieldName } from "../state/search";

interface SuggestionListProps {
  heading: string;
  suggestions: Suggestion[];
  onGoToField: (field: FieldName) => void;
}

/** Server suggestions for refining the ticket, each with what it is based on. */
export function SuggestionList({ heading, suggestions, onGoToField }: SuggestionListProps) {
  if (suggestions.length === 0) {
    return null;
  }
  return (
    <section className="suggestions" aria-label={heading}>
      <h3 className="section-heading">{heading}</h3>
      <ul className="suggestion-list">
        {suggestions.map((suggestion) => {
          const field = suggestion.field;
          return (
            <li key={suggestion.id} className="suggestion">
              <p className="suggestion-message">{suggestion.message}</p>
              <p className="basis">Based on: {suggestion.basis}</p>
              {field ? (
                <button type="button" className="button button-small" onClick={() => onGoToField(field)}>
                  Go to field: {FIELD_LABELS[field]}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
      <p className="muted">Edit the ticket and press Search again. Nothing is searched until you do.</p>
    </section>
  );
}
