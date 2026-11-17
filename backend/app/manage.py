"""Operator commands. Run from the backend folder, e.g. `uv run python -m app.manage migrate`.

    migrate                      apply the PostgreSQL migrations (assistant_* tables only)
    import-json FILE [--dry-run] copy validated tickets from a JSON file into PostgreSQL, idempotently
    check                        load tickets from the configured store and report what passes validation
    refresh-index [--url U] [--full]
                                 ask a running server to pick up saved or externally changed tickets

Nothing here seeds a database automatically. Credentials come from DATABASE_URL
and are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import BACKEND_DIR, get_settings
from .repository import RepositoryError, create_repository
from .tickets import TicketDataError, load_corpus


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def migrate(revision: str = "head", database_url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    if database_url:
        config.attributes["database_url"] = database_url
    command.upgrade(config, revision)


def import_json(path: Path, dry_run: bool, settings=None) -> dict:
    """Insert valid tickets that are not in the database yet. Existing rows are never changed."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.exc import SQLAlchemyError

    from .repository.postgres import PostgresTicketRepository, record_to_row, row_to_raw, tickets
    from .tickets import parse_record

    settings = settings or get_settings()
    if settings.ticket_store != "postgresql":
        raise SystemExit("TICKET_STORE must be postgresql for import-json.")
    try:
        corpus = load_corpus(path, frozenset())  # every valid record, whatever its status
    except TicketDataError as exc:
        raise SystemExit(f"Cannot import: {exc}") from None
    repo = PostgresTicketRepository(settings)
    summary = {
        "file": str(path),
        "dry_run": dry_run,
        "records_in_file": corpus.report.records_seen,
        "valid": len(corpus.tickets),
        "rejected": dict(corpus.report.rejected),
        "inserted" if not dry_run else "would_insert": 0,
        "already_present_identical": 0,
        "conflicts_not_overwritten": [],
    }
    key = "would_insert" if dry_run else "inserted"
    try:
        with repo.engine.begin() as conn:
            for record in corpus.tickets:
                row = conn.execute(select(tickets).where(tickets.c.ticket_id == record.ticket_id)).mappings().first()
                if row is not None:
                    existing = parse_record(row_to_raw(row), frozenset())
                    if existing is not None and existing.content() == record.content():
                        summary["already_present_identical"] += 1
                    else:
                        summary["conflicts_not_overwritten"].append(record.ticket_id)
                    continue
                if not dry_run:
                    conn.execute(
                        pg_insert(tickets)
                        .values(ticket_id=record.ticket_id, **record_to_row(record), version=1, index_status="pending")
                        .on_conflict_do_nothing(index_elements=["ticket_id"])
                    )
                summary[key] += 1
    except SQLAlchemyError as exc:
        raise SystemExit(f"Database error during import ({type(exc).__name__}). Nothing was committed.") from None
    finally:
        repo.close()
    return summary


def check(settings=None) -> dict:
    settings = settings or get_settings()
    try:
        repo = create_repository(settings)
        corpus = repo.load_corpus(settings.searchable_status_set)
        unindexed = repo.unindexed(settings.searchable_status_set)
    except (RepositoryError, TicketDataError) as exc:
        return {"ticket_store": settings.ticket_store, "ok": False, "error": str(exc)}
    report = corpus.report
    repo.close()
    return {
        "ticket_store": settings.ticket_store,
        "ok": bool(corpus.tickets),
        "searchable_tickets": len(corpus.tickets),
        "records_seen": report.records_seen,
        "rejected": dict(report.rejected),
        "warnings": dict(report.warnings),
        "categories": corpus.categories,
        "unindexed_ticket_ids": unindexed,
    }


def refresh_index(url: str, full: bool) -> dict:
    target = url.rstrip("/") + "/api/index/refresh" + ("?full=true" if full else "")
    request = urllib.request.Request(target, method="POST", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"The server answered {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {url}: {exc.reason}") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    importer = commands.add_parser("import-json", help="import a JSON ticket file into PostgreSQL")
    importer.add_argument("file", type=Path)
    importer.add_argument("--dry-run", action="store_true", help="validate and compare only; write nothing")
    commands.add_parser("check", help="validate the tickets in the configured store")
    refresher = commands.add_parser("refresh-index", help="ask a running server to refresh its search index")
    refresher.add_argument("--url", default="http://127.0.0.1:8080")
    refresher.add_argument("--full", action="store_true", help="rebuild even if no change is detected")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate()
        _print({"migrated": True})
    elif args.command == "import-json":
        _print(import_json(args.file, args.dry_run))
    elif args.command == "check":
        result = check()
        _print(result)
        if not result["ok"]:
            sys.exit(1)
    elif args.command == "refresh-index":
        _print(refresh_index(args.url, args.full))


if __name__ == "__main__":
    main()
