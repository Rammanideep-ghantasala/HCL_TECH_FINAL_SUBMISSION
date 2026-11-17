"""Saving resolved tickets and keeping the search snapshot current.

Searching never calls this module. Index state per saved ticket is tracked in
the store, separately from the save itself:

    pending   saved; not yet confirmed in the search index
    indexed   saved and searchable
    failed    saved; the last indexing attempt failed (retry with refresh)

A save is: apply the save rules in the repository (atomic, durable), then
build a complete new search snapshot off to the side, embedding only new or
changed text, and swap it in only if it is whole. A failed build leaves the
saved ticket saved and the previous snapshot serving searches.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .engine import SearchEngine, embedding_cache_dir
from .repository import (
    INDEX_INDEXED,
    RepositoryError,
    SaveInput,
    SaveRefused,
    TicketRepository,
)
from .retrieval.semantic import Embedder, persist_index
from .tickets import TicketRecord

logger = logging.getLogger("app.service")


class TicketService:
    def __init__(
        self,
        settings: Settings,
        repository: TicketRepository,
        get_engine: Callable[[], SearchEngine],
        set_engine: Callable[[SearchEngine], None],
        embedder_factory: Callable[[Settings], Embedder] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._get_engine = get_engine
        self._set_engine = set_engine
        self._embedder_factory = embedder_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    @property
    def statuses(self) -> frozenset[str]:
        return self.settings.searchable_status_set

    @property
    def save_enabled(self) -> bool:
        return self.settings.save_enabled and self.repository.writable

    # ------------------------------------------------------------- reading
    def get(self, ticket_id: str) -> tuple[TicketRecord, str, bool] | None:
        """(stored record, stored index status, live in the current search snapshot)."""
        found = self.repository.get(ticket_id)
        if found is None:
            return None
        record, status = found
        return record, status, self._get_engine().has_revision(record.ticket_id, record.revision)

    def unindexed_ticket_ids(self) -> list[str]:
        try:
            return self.repository.unindexed(self.statuses)
        except RepositoryError:
            return []

    # -------------------------------------------------------------- saving
    def save(self, request: SaveInput) -> dict:
        if not self.settings.save_enabled:
            raise SaveRefused(403, "save_disabled", "Saving resolved tickets is switched off on this server.")
        if not self.repository.writable:
            raise SaveRefused(503, "store_unavailable", "The ticket store cannot be written. Nothing was saved.")
        with self._lock:
            try:
                result = self.repository.save(request, self._clock(), self.statuses)
            except RepositoryError as exc:
                raise SaveRefused(503, exc.code, "The ticket store is unavailable. Nothing was saved.") from exc
            record = result.record
            if self._get_engine().has_revision(record.ticket_id, record.revision):
                indexing = self._indexed_status(self._get_engine())
                self._mark_indexed_quietly()
            else:
                indexing = self._rebuild()
        lead = {
            "created": f"Ticket {record.ticket_id} was saved.",
            "updated": f"Ticket {record.ticket_id} was updated.",
            "unchanged": f"Ticket {record.ticket_id} was already saved with this content; nothing new was written.",
        }[result.outcome]
        stored = self.get(record.ticket_id)
        return {
            "outcome": result.outcome,
            "repeat_submission": result.repeat_submission,
            "record": record,
            "index_status": stored[1] if stored else None,
            "indexed": stored[2] if stored else False,
            "indexing": indexing,
            "message": f"{lead} {indexing['message']}",
        }

    # ------------------------------------------------------------ indexing
    def refresh(self, full: bool = False) -> dict:
        """Rebuild when anything is pending or failed, the store changed, or `full` is asked.

        This is the retry path after an indexing failure and the way external
        database edits, deletions and reopened tickets reach searches.
        """
        with self._lock:
            engine = self._get_engine()
            try:
                pending = self.repository.unindexed(self.statuses)
                changed = engine.corpus is None or self.repository.change_marker() != engine.corpus.source_marker
            except RepositoryError as exc:
                return self._failed(engine, "the ticket store could not be read", code=exc.code)
            if not (full or pending or changed or not engine.ready):
                return {**self._indexed_status(engine), "message": "The search index is already up to date."}
            return self._rebuild()

    def recover_after_start(self) -> None:
        """Mark tickets that the start-up build indexed; leftovers stay pending or are marked failed."""
        engine = self._get_engine()
        if not engine.ready or engine.corpus is None:
            return
        with self._lock:
            self._mark_indexed_quietly()

    def _mark_indexed_quietly(self) -> None:
        engine = self._get_engine()
        if engine.corpus is None:
            return
        try:
            self.repository.mark_indexed(engine.corpus)
            leftover = [
                ticket_id
                for ticket_id in self.repository.unindexed(self.statuses)
                if ticket_id not in engine.corpus.by_id
            ]
            self.repository.mark_failed(leftover, "did not pass validation, so it is not searchable")
        except RepositoryError:
            logger.warning("index_status_not_recorded")

    def _rebuild(self) -> dict:
        started = time.perf_counter()
        current = self._get_engine()
        try:
            corpus = self.repository.load_corpus(self.statuses)
        except RepositoryError as exc:
            return self._failed(current, "the ticket store could not be read", code=exc.code)
        except Exception as exc:
            logger.error("index_rebuild_failed", extra={"fields": {"exception_type": type(exc).__name__}})
            return self._failed(current, "the saved tickets could not be loaded")
        try:
            candidate = SearchEngine.build(
                self.settings, embedder_factory=self._embedder_factory, corpus=corpus, previous=current
            )
        except Exception as exc:  # nothing is swapped; the saved data is untouched
            logger.error("index_rebuild_failed", extra={"fields": {"exception_type": type(exc).__name__}})
            return self._failed(current, "the search index could not be rebuilt")
        reason = None
        if not candidate.ready:
            reason = "the rebuilt search index is not usable"
        elif current.keyword is not None and candidate.keyword is None:
            reason = "keyword search could not index the saved tickets"
        elif current.semantic is not None and candidate.semantic is None:
            reason = "the embedding model could not process the saved text"
        if reason:
            return self._failed(current, reason)

        self._set_engine(candidate)
        self._mark_indexed_quietly()
        if candidate.semantic is not None:
            try:
                persist_index(candidate.semantic, corpus, embedding_cache_dir(self.settings, corpus.source_id))
            except Exception as exc:  # only a cache: searching already works
                logger.warning("embedding_cache_not_written", extra={"fields": {"exception_type": type(exc).__name__}})
        logger.info(
            "index_rebuilt",
            extra={
                "fields": {
                    "tickets": len(corpus.tickets),
                    "embeddings_computed": candidate.semantic.computed_rows if candidate.semantic else None,
                    "index_version": candidate.index_version,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
            },
        )
        return self._indexed_status(candidate)

    def _failed(self, current: SearchEngine, reason: str, code: str | None = None) -> dict:
        logger.error("index_not_updated", extra={"fields": {"reason": reason, "code": code}})
        try:
            self.repository.mark_failed(self.repository.unindexed(self.statuses), reason)
        except RepositoryError:
            pass
        return {
            "status": "failed",
            "searchable": False,
            "message": (
                f"It is saved but not yet available to future searches: {reason}. "
                "Searches still use the previous index. Retry indexing."
            ),
            "index_version": current.index_version,
        }

    @staticmethod
    def _indexed_status(engine: SearchEngine) -> dict:
        if engine.mode == "keyword_only":
            message = "It is available for future searches (keyword search only; meaning-based search is unavailable)."
        else:
            message = "It is available for future searches."
        return {"status": INDEX_INDEXED, "searchable": True, "message": message, "index_version": engine.index_version}


class RefreshPoller:
    """Optional: checks for store changes every interval so several processes stay in step."""

    def __init__(self, service: TicketService, interval_seconds: int) -> None:
        self.service = service
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="index-refresh", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.service.refresh()
            except Exception as exc:
                logger.error("scheduled_refresh_failed", extra={"fields": {"exception_type": type(exc).__name__}})
