"""PostgreSQL mode: every ticket comes from the database. There is no JSON fallback.

Tables are created only by the migrations in backend/migrations (application-
owned names prefixed "assistant_"). Nothing here creates, renames or drops a
table. If the database or the schema is missing, start-up reports a database
error and search is unavailable.

Correctness under concurrency comes from the database:
* ticket_id is the primary key, so a ticket is inserted at most once;
* a save locks the ticket row (SELECT ... FOR UPDATE) before comparing
  revisions, so a stale update cannot slip in between the check and the write;
* the submission id is a primary key written in the same transaction, so two
  concurrent copies of one submission produce one record;
* a trigger bumps `version`, sets `updated_at` and marks the row "pending"
  whenever ticket content changes, including edits made outside the app.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from ..config import Settings
from ..tickets import TicketCorpus, TicketRecord, corpus_from_records, parse_record
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

logger = logging.getLogger("app.repository.postgres")

VERSION_TABLE = "assistant_schema_version"
metadata = MetaData()

tickets = Table(
    "assistant_tickets",
    metadata,
    Column("ticket_id", String(64), primary_key=True),
    Column("subject", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("resolution_notes", Text, nullable=False),
    Column("category", Text),
    Column("tags", ARRAY(Text), nullable=False),
    Column("created_at", Date),
    Column("status", Text, nullable=False),
    Column("product", Text),
    Column("error_detail", Text),
    Column("investigation", Text),
    Column("root_cause", Text),
    Column("verification", Text),
    Column("resolved_by", Text),
    Column("resolved_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("index_status", Text, nullable=False),
    Column("index_error", Text),
    Column("indexed_version", Integer),
)

submissions = Table(
    "assistant_submissions",
    metadata,
    Column("submission_id", String(64), primary_key=True),
    Column("ticket_id", String(64), nullable=False),
    Column("revision", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_CONTENT_COLUMNS = (
    "subject", "description", "resolution_notes", "category", "tags", "created_at", "status", "product",
    "error_detail", "investigation", "root_cause", "verification", "resolved_by", "resolved_at",
)


class _Retry(Exception):
    """Another transaction changed the same row or submission first; decide again."""


def make_engine(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise RepositoryError("DATABASE_URL is not set")
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "application_name": "ticket-similarity-assistant",
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms} -c lock_timeout=5000",
        },
    )


def source_description(settings: Settings) -> str:
    """Host, port and database name only: never the user or password."""
    if settings.database_url is None:
        return "postgresql (not configured)"
    url = make_url(settings.database_url.get_secret_value())
    return f"{url.host}:{url.port or 5432}/{url.database}"


def _format_date(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else value


def _format_time(value) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def row_to_raw(row) -> dict:
    """A database row in the loader's raw form, so the same validation applies."""
    return {
        "ticket_id": row["ticket_id"],
        "subject": row["subject"],
        "description": row["description"],
        "resolution_notes": row["resolution_notes"],
        "category": row["category"],
        "tags": list(row["tags"] or []),
        "created_at": _format_date(row["created_at"]),
        "status": row["status"],
        "product": row["product"],
        "error_detail": row["error_detail"],
        "investigation": row["investigation"],
        "root_cause": row["root_cause"],
        "verification": row["verification"],
        "resolved_by": row["resolved_by"],
        "resolved_at": _format_time(row["resolved_at"]),
    }


def record_to_row(record: TicketRecord) -> dict:
    raw = record_to_raw(record)
    values = {key: raw[key] for key in _CONTENT_COLUMNS}
    values["created_at"] = date.fromisoformat(raw["created_at"]) if raw["created_at"] else None
    values["resolved_at"] = (
        datetime.strptime(raw["resolved_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if raw["resolved_at"]
        else None
    )
    return values


class PostgresTicketRepository(TicketRepository):
    kind = "postgresql"

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or make_engine(settings)
        self.batch_size = settings.db_load_batch_size
        self.source = source_description(settings)
        self.source_id = "postgresql-" + hashlib.sha256(self.source.encode("utf-8")).hexdigest()[:12]

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _database_error(exc: Exception) -> RepositoryError:
        # Only the exception type is kept: driver messages can contain host or user names.
        if isinstance(exc, ProgrammingError) and "assistant_" in str(getattr(exc, "orig", "")):
            error = RepositoryError("the database schema is missing; run the migrations")
            error.code = "database_schema_missing"
        else:
            error = RepositoryError(f"the database is unavailable ({type(exc).__name__})")
            error.code = "database_unavailable"
        return error

    # -------------------------------------------------------------- reading
    def _marker(self, conn) -> str:
        count, total, latest = conn.execute(
            select(func.count(), func.coalesce(func.sum(tickets.c.version), 0), func.max(tickets.c.updated_at))
        ).one()
        return f"{count}:{total}:{latest.isoformat() if latest else '-'}"

    def change_marker(self) -> str:
        try:
            with self.engine.connect() as conn:
                return self._marker(conn)
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc

    def load_corpus(self, statuses: frozenset[str]) -> TicketCorpus:
        """Eligible tickets, read in batches inside one consistent snapshot."""
        raws: list[dict] = []
        versions: dict[str, int] = {}
        eligible = [status.lower() for status in statuses]
        try:
            with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                with conn.begin():
                    marker = self._marker(conn)
                    after = ""
                    while True:
                        query = (
                            select(tickets)
                            .where(func.lower(tickets.c.status).in_(eligible))
                            .where(func.btrim(tickets.c.resolution_notes) != "")
                            .where(tickets.c.ticket_id > after)
                            .order_by(tickets.c.ticket_id)
                            .limit(self.batch_size)
                        )
                        rows = conn.execute(query).mappings().all()
                        if not rows:
                            break
                        for row in rows:
                            raws.append(row_to_raw(row))
                            versions[row["ticket_id"]] = row["version"]
                        after = rows[-1]["ticket_id"]
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc
        corpus = corpus_from_records(raws, statuses, source_name=f"postgresql:{self.source}")
        return replace(corpus, source_id=self.source_id, source_marker=marker, row_versions=versions)

    def get(self, ticket_id: str) -> tuple[TicketRecord, str] | None:
        try:
            with self.engine.connect() as conn:
                row = conn.execute(select(tickets).where(tickets.c.ticket_id == ticket_id)).mappings().first()
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc
        if row is None:
            return None
        record = parse_record(row_to_raw(row), frozenset())
        return (record, row["index_status"]) if record else None

    # -------------------------------------------------------------- saving
    def save(self, request: SaveInput, now: datetime, statuses: frozenset[str]) -> SaveResult:
        for _attempt in range(4):
            try:
                return self._save_once(request, now, statuses)
            except (_Retry, IntegrityError):
                continue  # another transaction won a race; decide again against its result
            except SaveRefused:
                raise
            except SQLAlchemyError as exc:
                logger.error("database_save_failed", extra={"fields": {"exception_type": type(exc).__name__}})
                raise SaveRefused(
                    503, "database_unavailable", "The database could not save the ticket. Nothing was saved."
                ) from exc
        raise SaveRefused(409, "save_conflict", "The ticket kept changing while saving. Nothing was saved. Try again.")

    def _save_once(self, request: SaveInput, now: datetime, statuses: frozenset[str]) -> SaveResult:
        with self.engine.begin() as conn:
            prior = conn.execute(
                select(submissions.c.ticket_id).where(submissions.c.submission_id == request.submission_id)
            ).scalar_one_or_none()

            def lookup(ticket_id: str) -> TicketRecord | None:
                row = (
                    conn.execute(select(tickets).where(tickets.c.ticket_id == ticket_id).with_for_update())
                    .mappings()
                    .first()
                )
                return parse_record(row_to_raw(row), frozenset()) if row else None

            plan = plan_save(request, lookup=lookup, prior_ticket_id=prior, now=now, statuses=statuses)
            if plan.action == "repeat":
                return SaveResult("unchanged", plan.record, True)
            if plan.action == "insert":
                inserted = conn.execute(
                    pg_insert(tickets)
                    .values(
                        ticket_id=plan.ticket_id,
                        **record_to_row(plan.record),
                        updated_at=func.now(),
                        version=1,
                        index_status=INDEX_PENDING,
                    )
                    .on_conflict_do_nothing(index_elements=["ticket_id"])
                    .returning(tickets.c.ticket_id)
                ).scalar_one_or_none()
                if inserted is None:  # another transaction inserted this id first
                    raise _Retry()
            elif plan.action == "update":
                conn.execute(
                    update(tickets).where(tickets.c.ticket_id == plan.ticket_id).values(**record_to_row(plan.record))
                )
            conn.execute(
                insert(submissions).values(
                    submission_id=request.submission_id,
                    ticket_id=plan.ticket_id,
                    revision=plan.record.revision,
                    created_at=func.now(),
                )
            )
        outcome = {"insert": "created", "update": "updated", "unchanged": "unchanged"}[plan.action]
        return SaveResult(outcome, plan.record, False)

    # ------------------------------------------------------- index status
    def mark_indexed(self, corpus: TicketCorpus) -> None:
        try:
            with self.engine.begin() as conn:
                waiting = conn.execute(
                    select(tickets.c.ticket_id, tickets.c.version).where(tickets.c.index_status != INDEX_INDEXED)
                ).all()
                for ticket_id, version in waiting:
                    if corpus.row_versions.get(ticket_id) == version:
                        conn.execute(
                            update(tickets)
                            .where(tickets.c.ticket_id == ticket_id)
                            .where(tickets.c.version == version)
                            .values(index_status=INDEX_INDEXED, index_error=None, indexed_version=version)
                        )
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc

    def mark_failed(self, ticket_ids: list[str], reason: str) -> None:
        if not ticket_ids:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    update(tickets)
                    .where(tickets.c.ticket_id.in_(ticket_ids))
                    .where(tickets.c.index_status != INDEX_INDEXED)
                    .values(index_status=INDEX_FAILED, index_error=reason[:500])
                )
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc

    def unindexed(self, statuses: frozenset[str]) -> list[str]:
        eligible = [status.lower() for status in statuses]
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    select(tickets.c.ticket_id)
                    .where(tickets.c.index_status != INDEX_INDEXED)
                    .where(func.lower(tickets.c.status).in_(eligible))
                    .order_by(tickets.c.ticket_id)
                    .limit(1000)
                ).scalars()
                return list(rows)
        except SQLAlchemyError as exc:
            raise self._database_error(exc) from exc
