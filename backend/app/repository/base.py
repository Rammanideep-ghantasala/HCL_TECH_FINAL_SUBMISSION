"""The ticket-repository interface, and the save rules every store applies.

Search never calls a repository. Repositories are used at start-up, on an
explicit index refresh, and when an agent explicitly saves a resolved ticket.

Save rules (identical for JSON and PostgreSQL; each store applies them inside
its own lock or transaction so they hold under concurrent requests):

* A submission id that was already saved returns that result again. Nothing
  new is written. The same submission id with different content is refused.
* No ticket id: a new id is generated. A new case is never merged with an
  existing ticket, even when the text is identical.
* A ticket id that does not exist yet: inserted once.
* A ticket id that exists with identical content: nothing changes.
* A ticket id that exists with different content: updated only when the
  request carries the revision the agent reviewed and it is still current.
  Otherwise the save is refused as a conflict and nothing is written.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..text import exact_text
from ..tickets import TicketCorpus, TicketRecord, parse_record

INDEX_PENDING = "pending"
INDEX_INDEXED = "indexed"
INDEX_FAILED = "failed"


class RepositoryError(Exception):
    """The ticket store cannot be reached, read or written. A service error, never a search result."""

    code = "ticket_store_unavailable"


class SaveRefused(Exception):
    """The save was understood but not applied. Nothing was written."""

    def __init__(self, status_code: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field


@dataclass
class SaveInput:
    submission_id: str
    subject: str
    description: str
    category: str
    investigation: str
    resolution_steps: str
    verification: str
    resolved_by: str
    ticket_id: str | None = None
    expected_revision: str | None = None
    product: str | None = None
    error_detail: str | None = None
    root_cause: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class SaveResult:
    outcome: str  # "created", "updated" or "unchanged"
    record: TicketRecord
    repeat_submission: bool


@dataclass
class SavePlan:
    """What a save will do, decided against the store's current state."""

    action: str  # "repeat", "unchanged", "insert" or "update"
    ticket_id: str
    record: TicketRecord  # the record to write, or the existing one for "repeat"/"unchanged"


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = exact_text(value)
    return cleaned or None


def build_record(
    request: SaveInput, ticket_id: str, existing: TicketRecord | None, now: datetime, statuses: frozenset[str]
) -> TicketRecord:
    raw = {
        "ticket_id": ticket_id,
        "subject": _text(request.subject) or "",
        "description": _text(request.description) or "",
        "category": _text(request.category),
        "product": _text(request.product),
        "error_detail": _text(request.error_detail),
        "investigation": _text(request.investigation),
        "root_cause": _text(request.root_cause),
        "resolution_notes": _text(request.resolution_steps),
        "verification": _text(request.verification),
        "resolved_by": _text(request.resolved_by),
        "tags": [tag for tag in (_text(t) for t in request.tags) if tag],
        "status": "resolved",
        # A new case is created now; an updated ticket keeps its original date.
        "created_at": existing.created_at if existing and existing.created_at else now.strftime("%Y-%m-%d"),
        "resolved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    record = parse_record(raw, statuses)
    if record is None:
        raise SaveRefused(422, "validation_error", "The ticket did not pass validation. Nothing was saved.")
    return record


def record_to_raw(record: TicketRecord) -> dict:
    """The stored form. Subject and description keep the agent's own text."""
    return {
        "ticket_id": record.ticket_id,
        "subject": record.exact_key[0],
        "description": record.exact_key[1],
        "category": record.category,
        "product": record.product,
        "error_detail": record.error_detail,
        "investigation": record.investigation,
        "root_cause": record.root_cause,
        "resolution_notes": record.resolution_notes,
        "verification": record.verification,
        "resolved_by": record.resolved_by,
        "tags": list(record.tags),
        "status": record.status,
        "created_at": record.created_at,
        "resolved_at": record.resolved_at,
    }


def new_ticket_id(now: datetime, taken: Callable[[str], bool]) -> str:
    while True:
        candidate = f"SR-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        if not taken(candidate):
            return candidate


def plan_save(
    request: SaveInput,
    *,
    lookup: Callable[[str], TicketRecord | None],
    prior_ticket_id: str | None,
    now: datetime,
    statuses: frozenset[str],
) -> SavePlan:
    """Apply the save rules. `lookup` must read the store's current, locked state."""
    if prior_ticket_id is not None:
        existing = lookup(prior_ticket_id)
        if existing is None:
            raise SaveRefused(409, "submission_conflict", "This form was saved before, but that ticket no longer exists.")
        requested = _text(request.ticket_id)
        candidate = build_record(request, existing.ticket_id, existing, now, statuses)
        if (requested is None or requested == existing.ticket_id) and candidate.content() == existing.content():
            return SavePlan("repeat", existing.ticket_id, existing)
        raise SaveRefused(
            409,
            "submission_conflict",
            f"This form was already saved as {existing.ticket_id} with different content. Nothing was saved.",
        )

    ticket_id = _text(request.ticket_id) or new_ticket_id(now, lambda candidate: lookup(candidate) is not None)
    existing = lookup(ticket_id)
    candidate = build_record(request, ticket_id, existing, now, statuses)
    if existing is None:
        return SavePlan("insert", ticket_id, candidate)
    if candidate.content() == existing.content():
        return SavePlan("unchanged", ticket_id, existing)
    if request.expected_revision is None or request.expected_revision != existing.revision:
        raise SaveRefused(
            409,
            "ticket_conflict",
            f"Ticket {ticket_id} already exists with different content"
            + (" and was changed after you reviewed it" if request.expected_revision else "")
            + ". Nothing was saved. Review the stored version, then replace it explicitly or save a new ticket.",
            field="ticket_id",
        )
    return SavePlan("update", ticket_id, candidate)


class TicketRepository(ABC):
    kind: str
    source_id: str  # identifies the store without credentials; scopes caches

    @property
    def writable(self) -> bool:
        return True

    @property
    def error(self) -> str | None:
        return None

    @abstractmethod
    def load_corpus(self, statuses: frozenset[str]) -> TicketCorpus:
        """Every eligible ticket, validated. Raises RepositoryError or TicketDataError."""

    @abstractmethod
    def change_marker(self) -> str:
        """Cheap value that changes whenever stored tickets change (including external edits)."""

    @abstractmethod
    def get(self, ticket_id: str) -> tuple[TicketRecord, str] | None:
        """The stored ticket and its index status, whether or not it passed into the search index."""

    @abstractmethod
    def save(self, request: SaveInput, now: datetime, statuses: frozenset[str]) -> SaveResult:
        """Apply the save rules atomically and durably. Saved tickets start as "pending" indexing."""

    @abstractmethod
    def mark_indexed(self, corpus: TicketCorpus) -> None:
        """Mark every stored ticket whose current version is in `corpus` as indexed."""

    @abstractmethod
    def mark_failed(self, ticket_ids: list[str], reason: str) -> None:
        """Record that indexing these tickets failed. Their saved content is not touched."""

    @abstractmethod
    def unindexed(self, statuses: frozenset[str]) -> list[str]:
        """Eligible tickets whose current version is not marked as indexed."""

    def close(self) -> None:
        return None
