interface TermListProps {
  terms: string[];
  /** Shown instead of the list when there are no terms. */
  emptyText?: string;
  label: string;
}

/** A wrapping list of short terms shown as text chips. */
export function TermList({ terms, emptyText, label }: TermListProps) {
  if (terms.length === 0) {
    return emptyText ? <p className="muted">{emptyText}</p> : null;
  }
  return (
    <ul className="chips" aria-label={label}>
      {terms.map((term, index) => (
        <li key={`${index}-${term}`} className="chip">
          {term}
        </li>
      ))}
    </ul>
  );
}
