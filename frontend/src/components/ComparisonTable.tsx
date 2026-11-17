import type { ComparisonRow } from "../api/types";

const OUTCOME_LABELS: Record<ComparisonRow["outcome"], string> = {
  same: "Same",
  different: "Different",
  partly_shared: "Partly shared",
  unknown: "Unknown",
};

function Values({ values, basis }: { values: string[]; basis: string }) {
  return (
    <>
      <span className="ticket-text">{values.length === 0 ? "Unknown" : values.join(", ")}</span>
      {basis ? <span className="basis cell-basis">{basis}</span> : null}
    </>
  );
}

interface ComparisonTableProps {
  rows: ComparisonRow[];
  ticketId: string;
}

/** The "available information" comparison. Unknown is shown as unknown, never as a mismatch. */
export function ComparisonTable({ rows, ticketId }: ComparisonTableProps) {
  if (rows.length === 0) {
    return <p className="muted">No structured information is available to compare.</p>;
  }
  return (
    <div
      className="table-scroll"
      role="region"
      aria-label={`Comparison of available information with ${ticketId}`}
      // A region that can scroll sideways must be reachable by keyboard.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="comparison">
        <thead>
          <tr>
            <th scope="col">Information</th>
            <th scope="col">New ticket</th>
            <th scope="col">Past ticket {ticketId}</th>
            <th scope="col">What this shows</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <th scope="row">{row.label}</th>
              <td>
                <Values values={row.new_values} basis={row.new_basis} />
              </td>
              <td>
                <Values values={row.past_values} basis={row.past_basis} />
              </td>
              <td>
                <span className={`outcome-tag outcome-${row.outcome}`}>
                  {OUTCOME_LABELS[row.outcome] ?? "Unknown"}
                </span>
                <span className="statement">{row.statement}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
