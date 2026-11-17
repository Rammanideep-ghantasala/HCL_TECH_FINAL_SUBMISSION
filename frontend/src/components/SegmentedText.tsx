import { Fragment } from "react";
import type { TextSegment } from "../api/types";

interface SegmentedTextProps {
  segments: TextSegment[];
  /** Shown when there is no text at all. */
  emptyText: string;
}

/**
 * Original text rebuilt from its segments. Matching runs become <mark>
 * elements; everything is rendered as plain text nodes.
 */
export function SegmentedText({ segments, emptyText }: SegmentedTextProps) {
  const hasText = segments.some((segment) => segment.text !== "");
  if (!hasText) {
    return <span className="muted">{emptyText}</span>;
  }
  return (
    <span className="ticket-text">
      {segments.map((segment, index) =>
        segment.match ? (
          <mark key={index}>{segment.text}</mark>
        ) : (
          <Fragment key={index}>{segment.text}</Fragment>
        ),
      )}
    </span>
  );
}
