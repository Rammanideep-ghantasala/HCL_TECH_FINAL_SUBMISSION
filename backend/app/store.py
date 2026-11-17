"""Durable store for tickets saved through the assistant.

One JSON file in the writable data folder, separate from the read-only seed
ticket file. Every write goes to a temporary file that is flushed to disk and
then renamed over the old file, so a crash leaves either the old or the new
version, never half of one. A file that cannot be read is never overwritten.

The file also remembers each submission id it has saved, so a repeated
submission (a double click, a retry after a timeout, a resend after a
restart) is recognised and never creates a second record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from pathlib import Path

STORE_FILE = "saved_tickets.json"
FORMAT_VERSION = 1


class StoreError(Exception):
    """The store file exists but cannot be trusted, or cannot be written."""


def store_path(data_dir: Path) -> Path:
    return data_dir / STORE_FILE


def _empty() -> dict:
    return {"format_version": FORMAT_VERSION, "tickets": [], "submissions": {}}


def read_store(path: Path) -> dict:
    """The store's content. A missing file is an empty store; a damaged one raises StoreError."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise StoreError(f"cannot read the saved-ticket store: {type(exc).__name__}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreError("the saved-ticket store is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format_version") != FORMAT_VERSION
        or not isinstance(payload.get("tickets"), list)
        or not all(isinstance(ticket, dict) for ticket in payload["tickets"])
        or not isinstance(payload.get("submissions"), dict)
    ):
        raise StoreError("the saved-ticket store has an unexpected shape")
    return payload


def read_saved_records(data_dir: Path) -> list[dict]:
    return read_store(store_path(data_dir))["tickets"]


class TicketStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = store_path(data_dir)
        self._lock = threading.Lock()
        self.error: str | None = None
        try:
            self._payload = read_store(self.path)
        except StoreError as exc:
            # Keep the damaged file untouched and refuse writes until it is fixed.
            self._payload = _empty()
            self.error = str(exc)

    @property
    def usable(self) -> bool:
        return self.error is None

    def records(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._payload["tickets"])

    def count(self) -> int:
        with self._lock:
            return len(self._payload["tickets"])

    def submission(self, submission_id: str) -> dict | None:
        with self._lock:
            entry = self._payload["submissions"].get(submission_id)
            return dict(entry) if isinstance(entry, dict) else None

    def digest(self) -> str:
        with self._lock:
            payload = json.dumps(self._payload["tickets"], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def apply(self, change) -> None:
        """Run `change(payload)` on a copy and write the result durably, all under one lock.

        The in-memory copy is replaced only after the file is safely on disk, so
        a failed write leaves both the file and memory as they were.
        """
        if not self.usable:
            raise StoreError(self.error or "the saved-ticket store is unusable")
        with self._lock:
            updated = copy.deepcopy(self._payload)
            if change(updated) is False:
                return
            self._write(updated)
            self._payload = updated

    def put(self, record: dict | None, submission_id: str, ticket_id: str, revision: str) -> None:
        """Insert or replace `record` by ticket_id and remember the submission, in one durable write.

        `record` None records only the submission (for an unchanged repeat).
        """

        def change(payload: dict) -> None:
            if record is not None:
                tickets = payload["tickets"]
                for index, existing in enumerate(tickets):
                    if existing.get("ticket_id") == ticket_id:
                        tickets[index] = record
                        break
                else:
                    tickets.append(record)
            payload["submissions"][submission_id] = {"ticket_id": ticket_id, "revision": revision}

        self.apply(change)

    def _write(self, payload: dict) -> None:
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if hasattr(os, "O_DIRECTORY"):  # make the rename itself durable (POSIX)
                directory = os.open(self.path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise StoreError(f"cannot write the saved-ticket store: {type(exc).__name__}") from exc
