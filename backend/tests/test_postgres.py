"""Integration tests against a real PostgreSQL database.

Run with a disposable database, for example:
    TEST_DATABASE_URL=postgresql+psycopg://tsa:tsa-local-dev-only@127.0.0.1:55432/tsa_test uv run pytest -m postgres

The tests migrate that database and empty the assistant_* tables before each
test. Never point TEST_DATABASE_URL at a database whose data you need.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.engine import SearchEngine, SearchInput
from app.main import create_app
from app.manage import import_json, migrate
from app.repository import SaveInput, SaveRefused
from app.repository.postgres import PostgresTicketRepository
from app.tickets import load_corpus

from .conftest import REAL_TICKETS, FakeEmbedder, make_settings
from .test_resolution_workflow import NEW_DESCRIPTION, NEW_SUBJECT, resolution

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

QUERIES = [
    ("Charged twice for one billing cycle", "Two identical charges on the card statement."),
    ("Dashboard never finishes loading", "The spinner keeps going even after a refresh."),
    ("Okta SSO lands on a blank page", "After signing in through Okta the app shows a white page."),
    ("Zorblax plasma conduit", "Vented xenon overnight."),
    ("CSV export missing recent days", "The export leaves out the last week of transactions."),
]


@pytest.fixture(scope="module")
def migrated():
    migrate(database_url=DATABASE_URL)
    yield


@pytest.fixture
def db(migrated):
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE assistant_tickets, assistant_submissions"))
    yield engine
    engine.dispose()


def pg_settings(tmp_path, **overrides):
    values = {"ticket_store": "postgresql", "database_url": DATABASE_URL, **overrides}
    return make_settings(tmp_path, REAL_TICKETS, **values)


def seed(tmp_path) -> None:
    summary = import_json(REAL_TICKETS, dry_run=False, settings=pg_settings(tmp_path))
    assert summary["inserted"] == 40


class PgApp:
    def __init__(self, tmp_path, embedder: FakeEmbedder | None = None, **overrides):
        self.embedder = embedder or FakeEmbedder()
        self.settings = pg_settings(tmp_path, **overrides)
        self.client = TestClient(create_app(self.settings, embedder_factory=lambda _s: self.embedder))
        self.client.__enter__()

    def close(self):
        self.client.__exit__(None, None, None)

    def search(self, subject, description=""):
        response = self.client.post("/api/search", json={"subject": subject, "description": description})
        assert response.status_code == 200, response.text
        return response.json()


def _summary(outcome: dict) -> tuple:
    cards = outcome["results"] + outcome["related"]
    return (
        outcome["status"],
        tuple(c["ticket"]["ticket_id"] for c in cards),
        tuple((c["scores"]["keyword_cosine"], c["scores"]["semantic_cosine"]) for c in cards),
    )


# ----------------------------------------------------------- equivalence
def test_search_is_equivalent_between_json_and_postgresql(db, tmp_path):
    seed(tmp_path)
    factory = lambda _s: FakeEmbedder()  # noqa: E731
    json_engine = SearchEngine.build(make_settings(tmp_path, REAL_TICKETS), embedder_factory=factory)
    pg_engine = SearchEngine.build(pg_settings(tmp_path), embedder_factory=factory)
    assert pg_engine.ready and pg_engine.corpus.source_id.startswith("postgresql-")
    assert pg_engine.corpus.content_sha256 == json_engine.corpus.content_sha256
    assert pg_engine.index_version == json_engine.index_version
    for subject, description in QUERIES:
        query = SearchInput(subject=subject, description=description)
        assert _summary(pg_engine.search(query)) == _summary(json_engine.search(query)), subject


def test_database_url_is_not_exposed(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        for path in ("/api/meta", "/ready", "/api/tickets/T-1001"):
            body = app.client.get(path).text
            assert "tsa-local-dev-only" not in body and "postgresql+psycopg" not in body
        assert app.client.get("/api/meta").json()["ticket_store"] == "postgresql"
    finally:
        app.close()


# ------------------------------------------------------------ read-only
def test_searching_never_writes_to_the_database(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        def snapshot():
            with db.connect() as conn:
                tickets = conn.execute(text("SELECT ticket_id, version, updated_at FROM assistant_tickets ORDER BY 1")).all()
                submissions = conn.execute(text("SELECT count(*) FROM assistant_submissions")).scalar()
            return tickets, submissions

        before = snapshot()
        for _ in range(3):
            for subject, description in QUERIES:
                app.search(subject, description)
            stored = app.client.get("/api/tickets/T-1303").json()
            app.client.post(
                "/api/search", json={"subject": stored["subject"], "description": stored["description"], "ticket_id": "T-1303"}
            )
        assert snapshot() == before
    finally:
        app.close()


# ---------------------------------------------- startup indexing recovery
def test_imported_tickets_are_indexed_at_startup(db, tmp_path):
    seed(tmp_path)
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assistant_tickets WHERE index_status = 'pending'")).scalar() == 40
    app = PgApp(tmp_path)
    try:
        with db.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM assistant_tickets WHERE index_status = 'indexed'")).scalar() == 40
        assert app.client.get("/api/meta").json()["unindexed_ticket_ids"] == []
    finally:
        app.close()


# ---------------------------------------------------- saving and retrieval
def test_saved_resolution_persists_across_restart_and_is_retrievable(db, tmp_path):
    seed(tmp_path)
    first = PgApp(tmp_path)
    saved = first.client.post("/api/tickets", json=resolution())
    assert saved.status_code == 201, saved.text
    body = saved.json()
    ticket_id = body["ticket"]["ticket_id"]
    assert body["indexing"]["status"] == "indexed"
    assert first.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == [ticket_id]
    similar = first.search("Parquet bulk loader upload rejected", "Error LH-415 unsupported schema version on upload")
    assert ticket_id in [c["ticket"]["ticket_id"] for c in similar["results"] + similar["related"]]
    first.close()

    with db.connect() as conn:
        row = conn.execute(
            text("SELECT investigation, verification, index_status, version FROM assistant_tickets WHERE ticket_id = :id"),
            {"id": ticket_id},
        ).one()
    assert row.investigation.startswith("Compared the failing files") and row.index_status == "indexed"

    second = PgApp(tmp_path)
    try:
        stored = second.client.get(f"/api/tickets/{ticket_id}").json()
        assert stored["indexed"] is True and stored["verification"].startswith("All 12 re-run uploads")
        assert second.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == [ticket_id]
    finally:
        second.close()


def test_repeated_and_concurrent_submissions_make_one_record(db, tmp_path):
    seed(tmp_path)
    settings = pg_settings(tmp_path)
    repositories = [PostgresTicketRepository(settings) for _ in range(4)]  # like four app instances
    statuses = settings.searchable_status_set
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    request = SaveInput(**{k: v for k, v in resolution().items() if k not in ("confirm_resolved",)} | {"tags": ["parquet"]})
    barrier = threading.Barrier(4)
    outcomes: list = []

    def save(repository):
        barrier.wait()
        outcomes.append(repository.save(request, now, statuses))

    threads = [threading.Thread(target=save, args=(repo,)) for repo in repositories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for repository in repositories:
        repository.close()
    assert len({result.record.ticket_id for result in outcomes}) == 1
    assert sorted(result.outcome for result in outcomes) == ["created", "unchanged", "unchanged", "unchanged"]
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assistant_tickets WHERE subject = :s"), {"s": NEW_SUBJECT}).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM assistant_submissions")).scalar() == 1


def test_concurrent_conflicting_saves_to_one_new_id_keep_one_version(db, tmp_path):
    settings = pg_settings(tmp_path)
    repositories = [PostgresTicketRepository(settings) for _ in range(2)]
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    requests = [
        SaveInput(**{k: v for k, v in resolution(ticket_id="INC-900", verification=f"Check {n}.").items() if k != "confirm_resolved"})
        for n in range(2)
    ]
    barrier = threading.Barrier(2)
    results: list = []

    def save(repository, request):
        barrier.wait()
        try:
            results.append(repository.save(request, now, settings.searchable_status_set).outcome)
        except SaveRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=save, args=pair) for pair in zip(repositories, requests)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for repository in repositories:
        repository.close()
    assert sorted(results) == ["created", "ticket_conflict"]
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assistant_tickets WHERE ticket_id = 'INC-900'")).scalar() == 1


def test_stale_updates_are_rejected(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        original = app.client.get("/api/tickets/T-1303").json()
        blind = app.client.post("/api/tickets", json=resolution(ticket_id="T-1303"))
        assert blind.status_code == 409
        first = app.client.post("/api/tickets", json=resolution(ticket_id="T-1303", expected_revision=original["revision"]))
        assert first.status_code == 200 and first.json()["outcome"] == "updated"
        stale = app.client.post(
            "/api/tickets",
            json=resolution(ticket_id="T-1303", verification="Older edit.", expected_revision=original["revision"]),
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "ticket_conflict"
        # An edit made directly in the database also makes a held revision stale.
        current = app.client.get("/api/tickets/T-1303").json()
        with db.begin() as conn:
            conn.execute(text("UPDATE assistant_tickets SET verification = 'Edited by a DBA.' WHERE ticket_id = 'T-1303'"))
        after_external = app.client.post(
            "/api/tickets", json=resolution(ticket_id="T-1303", verification="Mine.", expected_revision=current["revision"])
        )
        assert after_external.status_code == 409
        assert app.client.get("/api/tickets/T-1303").json()["verification"] == "Edited by a DBA."
    finally:
        app.close()


def test_identical_text_from_another_incident_stays_separate(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        a = app.client.post("/api/tickets", json=resolution()).json()["ticket"]["ticket_id"]
        b = app.client.post("/api/tickets", json=resolution(resolution_steps="Asked for a re-export.")).json()["ticket"]["ticket_id"]
        assert a != b
        assert app.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == sorted([a, b])
    finally:
        app.close()


# ------------------------------------------------------- external changes
def test_external_edits_deletions_and_reopened_tickets_appear_after_refresh(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        chart = app.client.get("/api/tickets/T-1201").json()
        billing = app.client.get("/api/tickets/T-1303").json()
        widgets = app.client.get("/api/tickets/T-1202").json()
        exact = lambda t: app.search(t["subject"], t["description"])["exact_match"]["matched_ticket_ids"]  # noqa: E731
        assert exact(billing) == ["T-1303"] and exact(widgets) == ["T-1202"]
        with db.begin() as conn:
            conn.execute(text("UPDATE assistant_tickets SET subject = 'Quokka telemetry mirror drifts' WHERE ticket_id = 'T-1201'"))
            conn.execute(text("DELETE FROM assistant_tickets WHERE ticket_id = 'T-1202'"))
            conn.execute(text("UPDATE assistant_tickets SET status = 'reopened' WHERE ticket_id = 'T-1303'"))
        # Not visible until a refresh: searches use the published snapshot.
        assert app.client.get("/api/meta").json()["corpus_size"] == 40
        refreshed = app.client.post("/api/index/refresh").json()
        assert refreshed["indexing"]["status"] == "indexed"
        meta = app.client.get("/api/meta").json()
        assert meta["corpus_size"] == 38  # one deleted, one reopened
        assert exact({**chart, "subject": "Quokka telemetry mirror drifts"}) == ["T-1201"]
        assert exact(chart) == []  # the old subject no longer matches
        assert exact(billing) == [] and exact(widgets) == []
        ids = lambda o: [c["ticket"]["ticket_id"] for c in o["results"] + o["related"]]  # noqa: E731
        assert "T-1303" not in ids(app.search(billing["subject"], billing["description"]))
        assert "T-1202" not in ids(app.search(widgets["subject"], widgets["description"]))
        # A second refresh with nothing changed does no work.
        again = app.client.post("/api/index/refresh").json()
        assert again["indexing"]["message"] == "The search index is already up to date."
    finally:
        app.close()


# ------------------------------------------------------------- failures
def test_unreachable_database_is_a_database_error_not_a_no_match(db, tmp_path):
    settings_overrides = {"database_url": "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", "db_connect_timeout_seconds": 1}
    app = PgApp(tmp_path, **settings_overrides)
    try:
        ready = app.client.get("/ready")
        assert ready.status_code == 503 and ready.json()["checks"]["ticket_store"] is False
        search = app.client.post("/api/search", json={"subject": "Charged twice for the same billing cycle"})
        assert search.status_code == 503
        assert search.json()["error"]["code"] == "database_unavailable"
        assert "no_strong_match" not in search.text
        saved = app.client.post("/api/tickets", json=resolution())
        assert saved.status_code == 503
        assert app.client.get("/api/meta").json()["corpus_size"] == 0  # no JSON fallback
    finally:
        app.close()


def test_database_outage_after_start_fails_saves_but_not_searches(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        repository = app.client.app.state.repository
        repository.engine.dispose()
        repository.engine = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
        saved = app.client.post("/api/tickets", json=resolution())
        assert saved.status_code == 503 and saved.json()["error"]["code"] == "database_unavailable"
        assert "saved" not in saved.json()["error"]["message"].lower().replace("nothing was saved", "")
        stored = app.client.app.state.engine.corpus.by_id["T-1303"]
        found = app.search(stored.subject, stored.description)
        assert found["exact_match"]["matched_ticket_ids"] == ["T-1303"]
    finally:
        app.close()


def test_indexing_failure_keeps_the_saved_row_and_retry_recovers(db, tmp_path):
    seed(tmp_path)
    app = PgApp(tmp_path)
    try:
        app.embedder.fail_on_encode = True
        saved = app.client.post("/api/tickets", json=resolution()).json()
        ticket_id = saved["ticket"]["ticket_id"]
        assert saved["indexing"]["status"] == "failed" and "available for future searches" not in saved["message"]
        with db.connect() as conn:
            status = conn.execute(text("SELECT index_status FROM assistant_tickets WHERE ticket_id = :id"), {"id": ticket_id}).scalar()
        assert status == "failed"
        app.embedder.fail_on_encode = False
        assert app.client.post("/api/index/refresh").json()["indexing"]["status"] == "indexed"
        with db.connect() as conn:
            status = conn.execute(text("SELECT index_status FROM assistant_tickets WHERE ticket_id = :id"), {"id": ticket_id}).scalar()
        assert status == "indexed"
    finally:
        app.close()


def test_import_is_idempotent_and_never_overwrites(db, tmp_path):
    settings = pg_settings(tmp_path)
    dry = import_json(REAL_TICKETS, dry_run=True, settings=settings)
    assert dry["would_insert"] == 40
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assistant_tickets")).scalar() == 0
    assert import_json(REAL_TICKETS, dry_run=False, settings=settings)["inserted"] == 40
    with db.begin() as conn:
        conn.execute(text("UPDATE assistant_tickets SET resolution_notes = 'Local edit.' WHERE ticket_id = 'T-1001'"))
    again = import_json(REAL_TICKETS, dry_run=False, settings=settings)
    assert again["inserted"] == 0 and again["already_present_identical"] == 39
    assert again["conflicts_not_overwritten"] == ["T-1001"]
    with db.connect() as conn:
        assert conn.execute(text("SELECT resolution_notes FROM assistant_tickets WHERE ticket_id = 'T-1001'")).scalar() == "Local edit."
    assert len(load_corpus(REAL_TICKETS).tickets) == 40  # the JSON file itself is untouched
