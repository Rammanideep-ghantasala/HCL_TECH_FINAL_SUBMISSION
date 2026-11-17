"""Loading and validating the historical tickets and the example tickets.

The loader never guesses. A record that cannot be trusted as an answer source
is rejected with a reason code and counted; it is not repaired. Results are
always looked up by ticket id, so a rejected record cannot shift another
ticket's text or resolution.

Duplicate ids: the first record with an id is the one kept. A later record
that is identical to it is a no-op. A later record that differs is rejected
and reported, never applied: this service has no update workflow, so changing
a ticket means editing its one record. Records with different ids are always
kept as separate cases, even when their text is identical.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .text import clean_text, exact_text

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_ticket_id(value: str) -> bool:
    """Letters, digits, '.', '_' and '-', starting with a letter or digit, at most 64 characters."""
    return bool(_ID_RE.match(value))


class TicketDataError(Exception):
    """The ticket file is missing, unreadable or not shaped like a ticket file."""


@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    subject: str
    description: str
    resolution_notes: str
    category: str | None
    status: str | None
    tags: tuple[str, ...]
    created_at: str | None
    # (subject, description) from the source file, normalized only by
    # text.exact_text. Kept as two fields so no separator can make two
    # different tickets compare equal.
    exact_key: tuple[str, str]
    # Recorded by an agent through the resolution form. None for the seed tickets.
    product: str | None = None
    error_detail: str | None = None
    investigation: str | None = None
    root_cause: str | None = None
    verification: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None

    @property
    def problem_text(self) -> str:
        """The text that is searched: what the customer reported."""
        return f"{self.subject}\n{self.description}"

    @property
    def all_text(self) -> str:
        """Every stored word, used only to know which words the history contains."""
        parts = [
            self.subject, self.description, self.resolution_notes, " ".join(self.tags), self.category or "",
            self.product or "", self.error_detail or "", self.investigation or "", self.root_cause or "",
            self.verification or "",
        ]
        return "\n".join(parts)

    def content(self) -> list:
        """Everything a person wrote, without the timestamps the server adds."""
        return [
            self.ticket_id, self.subject, self.description, self.resolution_notes, self.category, self.status,
            list(self.tags), self.product, self.error_detail, self.investigation, self.root_cause,
            self.verification, self.resolved_by,
        ]

    @property
    def revision(self) -> str:
        """Changes whenever anything stored changes. Used to refuse blind overwrites."""
        payload = json.dumps(self.content() + [self.created_at, self.resolved_at], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoadReport:
    source: str
    records_seen: int = 0
    accepted: int = 0
    rejected: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    # Ids that appeared again with different content. The first record was kept.
    conflicting_ids: list[str] = field(default_factory=list)
    # Tickets saved through the assistant: how many were loaded, and how many
    # of those are explicit updates of a seed ticket with the same id.
    saved_accepted: int = 0
    saved_updates_of_seed: int = 0

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


@dataclass(frozen=True)
class TicketCorpus:
    tickets: tuple[TicketRecord, ...]
    by_id: dict[str, TicketRecord]
    sha256: str  # of the file bytes, for reporting
    report: LoadReport
    # Of the accepted records, independent of file order and of repeated
    # identical records. Unchanged content gives an unchanged value.
    content_sha256: str = ""
    # Which store the records came from (no credentials), and a cheap marker of
    # its state when they were read, used to notice later external changes.
    source_id: str = "json"
    source_marker: str = ""
    # Database row version per ticket id at load time (PostgreSQL only).
    row_versions: dict = field(default_factory=dict)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(ticket.ticket_id for ticket in self.tickets)

    @property
    def categories(self) -> list[str]:
        return sorted({ticket.category for ticket in self.tickets if ticket.category})


def _required_text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str):
        return None
    cleaned = clean_text(value).strip()
    return cleaned or None


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str):
        return None
    cleaned = clean_text(value).strip()
    return cleaned or None


def _parse_record(raw: Any, report: LoadReport, searchable_statuses: frozenset[str]) -> TicketRecord | None:
    if not isinstance(raw, dict):
        report.rejected["not_an_object"] += 1
        return None
    ticket_id = _required_text(raw, "ticket_id")
    if ticket_id is None or not _ID_RE.match(ticket_id):
        report.rejected["missing_or_invalid_id"] += 1
        return None
    subject = _required_text(raw, "subject")
    description = _required_text(raw, "description")
    if subject is None or description is None:
        report.rejected["missing_subject_or_description"] += 1
        return None
    resolution = _required_text(raw, "resolution_notes")
    if resolution is None:
        report.rejected["missing_resolution"] += 1
        return None
    status = _optional_text(raw, "status")
    if status is not None and searchable_statuses and status.lower() not in searchable_statuses:
        report.rejected["status_not_searchable"] += 1
        return None
    if status is None:
        report.warnings["missing_status"] += 1

    category = _optional_text(raw, "category")
    if category is None:
        report.warnings["missing_category"] += 1

    tags_raw = raw.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(tags_raw, list):
        tags = tuple(clean_text(tag).strip() for tag in tags_raw if isinstance(tag, str) and tag.strip())
    elif tags_raw is not None:
        report.warnings["invalid_tags"] += 1

    created_at = _optional_text(raw, "created_at")
    if created_at is not None and not _DATE_RE.match(created_at):
        report.warnings["invalid_date"] += 1
        created_at = None

    optional = {
        key: _optional_text(raw, key)
        for key in ("product", "error_detail", "investigation", "root_cause", "verification", "resolved_by", "resolved_at")
    }
    return TicketRecord(
        **optional,
        ticket_id=ticket_id,
        subject=subject,
        description=description,
        resolution_notes=resolution,
        category=category,
        status=status,
        tags=tags,
        created_at=created_at,
        exact_key=(exact_text(raw["subject"]), exact_text(raw["description"])),
    )


def parse_record(raw: dict, searchable_statuses: frozenset[str] = frozenset({"resolved"})) -> TicketRecord | None:
    """Validate one record with exactly the loader's rules. None when it would be rejected."""
    return _parse_record(raw, LoadReport(source="single"), searchable_statuses)


def _content_sha256(records: list[TicketRecord]) -> str:
    canonical = [
        record.content() + [record.created_at, record.resolved_at]
        for record in sorted(records, key=lambda record: record.ticket_id)
    ]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()


def _first_wins(records: list[TicketRecord], report: LoadReport) -> list[TicketRecord]:
    """One record per id: the first. Identical repeats are no-ops; changed repeats are rejected."""
    accepted: list[TicketRecord] = []
    first_by_id: dict[str, TicketRecord] = {}
    for record in records:
        first = first_by_id.get(record.ticket_id)
        if first is None:
            first_by_id[record.ticket_id] = record
            accepted.append(record)
        elif record == first:
            report.warnings["identical_duplicate"] += 1
        else:
            report.rejected["conflicting_duplicate_id"] += 1
            if record.ticket_id not in report.conflicting_ids:
                report.conflicting_ids.append(record.ticket_id)
    return accepted


def load_corpus(
    path: Path,
    searchable_statuses: frozenset[str] = frozenset({"resolved"}),
    saved_records: list | None = None,
) -> TicketCorpus:
    """Read and validate the historical ticket file, plus any tickets saved through the assistant.

    The seed file is never written. `saved_records` come from the separate
    store (see store.py); a saved record with the same id as a seed ticket is
    an explicit update made through the save workflow and replaces it.

    Raises TicketDataError when the file itself is unusable. Individual bad
    records are rejected and counted in the report.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise TicketDataError(f"cannot read ticket file: {type(exc).__name__}") from exc
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TicketDataError(f"ticket file is not valid UTF-8 JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        raise TicketDataError('ticket file must be an object with a "tickets" list')
    return corpus_from_records(
        payload["tickets"], searchable_statuses, saved_records,
        source_name=path.name, sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def corpus_from_records(
    records: list,
    searchable_statuses: frozenset[str] = frozenset({"resolved"}),
    saved_records: list | None = None,
    *,
    source_name: str,
    sha256: str = "",
) -> TicketCorpus:
    """Validate raw records from any store with the same rules, and assemble a corpus."""
    report = LoadReport(source=source_name)
    parsed: list[TicketRecord] = []
    for raw in records:
        report.records_seen += 1
        record = _parse_record(raw, report, searchable_statuses)
        if record is not None:
            parsed.append(record)

    # An id points at exactly one record: the first one. Repeating it unchanged
    # is a no-op; a changed repeat is rejected and reported, never applied.
    accepted = _first_wins(parsed, report)

    if saved_records:
        saved: list[TicketRecord] = []
        for raw in saved_records:
            report.records_seen += 1
            record = _parse_record(raw, report, searchable_statuses)
            if record is not None:
                saved.append(record)
        saved = _first_wins(saved, report)
        report.saved_accepted = len(saved)
        position = {record.ticket_id: index for index, record in enumerate(accepted)}
        for record in saved:
            if record.ticket_id in position:
                accepted[position[record.ticket_id]] = record
                report.saved_updates_of_seed += 1
            else:
                position[record.ticket_id] = len(accepted)
                accepted.append(record)

    # Same text under another id is a separate case. It is kept, and counted.
    seen_text: set[tuple[str, str]] = set()
    for record in accepted:
        if record.exact_key in seen_text:
            report.warnings["identical_text_different_id"] += 1
        seen_text.add(record.exact_key)

    report.accepted = len(accepted)
    return TicketCorpus(
        tickets=tuple(accepted),
        by_id={record.ticket_id: record for record in accepted},
        sha256=sha256,
        report=report,
        content_sha256=_content_sha256(accepted),
    )


@dataclass(frozen=True)
class ExampleTicket:
    ticket_id: str
    subject: str
    description: str


def load_examples(path: Path) -> list[ExampleTicket]:
    """Read the supplied example incoming tickets. Returns [] when unavailable."""
    try:
        payload = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    items = payload.get("new_tickets") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    examples: list[ExampleTicket] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        ticket_id = _required_text(raw, "ticket_id")
        subject = _optional_text(raw, "subject") or ""
        description = _optional_text(raw, "description") or ""
        if ticket_id and (subject or description):
            examples.append(ExampleTicket(ticket_id=ticket_id, subject=subject, description=description))
    return examples
