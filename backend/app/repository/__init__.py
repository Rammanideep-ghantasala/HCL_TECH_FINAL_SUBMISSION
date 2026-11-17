"""Ticket storage, kept separate from search. See base.py for the interface and save rules."""

from __future__ import annotations

from ..config import Settings
from .base import (
    INDEX_FAILED,
    INDEX_INDEXED,
    INDEX_PENDING,
    RepositoryError,
    SaveInput,
    SaveRefused,
    SaveResult,
    TicketRepository,
)


def create_repository(settings: Settings) -> TicketRepository:
    """The configured store. PostgreSQL never falls back to the JSON demonstration data."""
    if settings.ticket_store == "postgresql":
        from .postgres import PostgresTicketRepository

        return PostgresTicketRepository(settings)
    from .json_repo import JsonTicketRepository

    return JsonTicketRepository(settings.tickets_path, settings.data_dir)


__all__ = [
    "INDEX_FAILED",
    "INDEX_INDEXED",
    "INDEX_PENDING",
    "RepositoryError",
    "SaveInput",
    "SaveRefused",
    "SaveResult",
    "TicketRepository",
    "create_repository",
]
