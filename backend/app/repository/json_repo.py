"""JSON mode, for local demonstrations: the read-only seed file plus a JSON store.

The seed file (starter_kit/data/tickets.json) is never written. Tickets saved
through the assistant live in data_dir/saved_tickets.json (see store.py); one
with the same id as a seed ticket is an explicit update and replaces it.
Saves in this mode are serialized by one lock in one process; run a single
process (the default) when using it.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from pathlib import Path

from ..store import StoreError, TicketStore
from ..tickets import TicketCorpus, TicketDataError, TicketRecord, load_corpus, parse_record
from .base import (
    INDEX_FAILED,
    INDEX_INDEXED,
    INDEX_PENDING,
    RepositoryError,
    SaveInput,
    SaveRefused,
    SaveResult,
    TicketRepository,
    plan_save,
    record_to_raw,
)


class JsonTicketRepository(TicketRepository):
    kind = "json"

    def __init__(self, seed_path: Path, data_dir: Path) -> None:
        self.seed_path = seed_path
        self.store = TicketStore(data_dir)
        digest = hashlib.sha256(f"{seed_path.resolve()}|{data_dir.resolve()}".encode("utf-8")).hexdigest()[:12]
        self.source_id = f"json-{digest}"
        self._lock = threading.Lock()

    @property
    def writable(self) -> bool:
        return self.store.usable

    @property
    def error(self) -> str | None:
        return self.store.error

    # -------------------------------------------------------------- reading
    def load_corpus(self, statuses: frozenset[str]) -> TicketCorpus:
        corpus = load_corpus(self.seed_path, statuses, self.store.records())
        return _with_source(corpus, self.source_id, self.change_marker())

    def change_marker(self) -> str:
        try:
            seed = hashlib.sha256(self.seed_path.read_bytes()).hexdigest()[:16]
        except OSError as exc:
            raise RepositoryError("cannot read the ticket file") from exc
        return f"{seed}:{self.store.digest()}"

    def _view(self, statuses: frozenset[str]) -> dict[str, TicketRecord]:
        """Every valid stored ticket by id: the seed file overlaid with saved tickets."""
        try:
            return dict(load_corpus(self.seed_path, statuses, self.store.records()).by_id)
        except TicketDataError as exc:
            raise RepositoryError("cannot read the ticket file") from exc

    def get(self, ticket_id: str) -> tuple[TicketRecord, str] | None:
        for raw in self.store.records():
            if raw.get("ticket_id") == ticket_id:
                record = parse_record(raw, frozenset())
                return (record, raw.get("index_status", INDEX_PENDING)) if record else None
        record = self._view(frozenset({"resolved"})).get(ticket_id)
        return (record, INDEX_INDEXED) if record else None

    # -------------------------------------------------------------- saving
    def save(self, request: SaveInput, now: datetime, statuses: frozenset[str]) -> SaveResult:
        if not self.store.usable:
            raise SaveRefused(503, "store_unavailable", "The saved-ticket store cannot be used. Nothing was saved.")
        with self._lock:
            view = self._view(statuses)
            prior = self.store.submission(request.submission_id)
            plan = plan_save(
                request,
                lookup=view.get,
                prior_ticket_id=prior["ticket_id"] if prior else None,
                now=now,
                statuses=statuses,
            )
            if plan.action == "repeat":
                return SaveResult("unchanged", plan.record, True)
            try:
                if plan.action == "unchanged":
                    self.store.put(None, request.submission_id, plan.ticket_id, plan.record.revision)
                    return SaveResult("unchanged", plan.record, False)
                raw = {**record_to_raw(plan.record), "index_status": INDEX_PENDING, "index_error": None}
                self.store.put(raw, request.submission_id, plan.ticket_id, plan.record.revision)
            except StoreError as exc:
                raise SaveRefused(503, "store_unavailable", "The ticket could not be written. Nothing was saved.") from exc
            outcome = "created" if plan.action == "insert" else "updated"
            return SaveResult(outcome, plan.record, False)

    # ------------------------------------------------------- index status
    def mark_indexed(self, corpus: TicketCorpus) -> None:
        def change(payload: dict):
            changed = False
            for raw in payload["tickets"]:
                record = corpus.by_id.get(raw.get("ticket_id"))
                stored = parse_record(raw, frozenset())
                if record and stored and record.revision == stored.revision and raw.get("index_status") != INDEX_INDEXED:
                    raw["index_status"], raw["index_error"] = INDEX_INDEXED, None
                    changed = True
            return changed

        self._apply(change)

    def mark_failed(self, ticket_ids: list[str], reason: str) -> None:
        wanted = set(ticket_ids)

        def change(payload: dict):
            changed = False
            for raw in payload["tickets"]:
                if raw.get("ticket_id") in wanted and raw.get("index_status") != INDEX_INDEXED:
                    raw["index_status"], raw["index_error"] = INDEX_FAILED, reason
                    changed = True
            return changed

        self._apply(change)

    def unindexed(self, statuses: frozenset[str]) -> list[str]:
        return [
            raw["ticket_id"]
            for raw in self.store.records()
            if raw.get("index_status") != INDEX_INDEXED and str(raw.get("status", "")).lower() in statuses
        ]

    def _apply(self, change) -> None:
        try:
            self.store.apply(change)
        except StoreError as exc:
            raise RepositoryError("cannot update the saved-ticket store") from exc


def _with_source(corpus: TicketCorpus, source_id: str, marker: str) -> TicketCorpus:
    from dataclasses import replace

    return replace(corpus, source_id=source_id, source_marker=marker)
