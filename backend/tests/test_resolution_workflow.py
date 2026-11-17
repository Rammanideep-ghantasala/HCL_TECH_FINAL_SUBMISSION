"""Saving resolved tickets (JSON store) and making them searchable.

The PostgreSQL store runs the same scenarios in test_postgres.py.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FakeEmbedder, make_settings

NEW_SUBJECT = "Lakehouse loader rejects parquet uploads"
NEW_DESCRIPTION = "Uploading parquet files through the bulk loader fails with error LH-415 unsupported schema version."


def resolution(**overrides) -> dict:
    body = {
        "submission_id": str(uuid.uuid4()),
        "subject": NEW_SUBJECT,
        "description": NEW_DESCRIPTION,
        "category": "sync",
        "product": "Bulk loader",
        "error_detail": "LH-415",
        "investigation": "Compared the failing files with accepted ones; all failing files used schema version 3.",
        "root_cause": "The loader only accepted schema version 2.",
        "resolution_steps": "Enabled schema version 3 parsing for the workspace and re-ran the failed uploads.",
        "verification": "All 12 re-run uploads completed and the row counts matched the source files.",
        "resolved_by": "Support agent on shift B",
        "tags": ["parquet", "bulk-loader"],
        "confirm_resolved": True,
    }
    body.update(overrides)
    return body


class App:
    """A running app plus its embedder, so a test can make the model fail on demand."""

    def __init__(self, tmp_path, embedder: FakeEmbedder | None = None, **overrides):
        self.embedder = embedder or FakeEmbedder()
        self.settings = make_settings(tmp_path, overrides.pop("tickets_path"), **overrides)
        self.client = TestClient(create_app(self.settings, embedder_factory=lambda _s: self.embedder))
        self.client.__enter__()

    def close(self):
        self.client.__exit__(None, None, None)

    def search(self, subject: str, description: str = "") -> dict:
        response = self.client.post("/api/search", json={"subject": subject, "description": description})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def app(tmp_path, sample_tickets_path):
    running = App(tmp_path, tickets_path=sample_tickets_path)
    yield running
    running.close()


def _ids(outcome: dict) -> list[str]:
    return [card["ticket"]["ticket_id"] for card in outcome["results"] + outcome["related"]]


# ------------------------------------------------------------- validation
def test_required_resolution_fields_are_validated(app):
    seed_file = app.settings.tickets_path.read_bytes()
    body = resolution(
        subject=" ", category="", investigation="", resolution_steps="  ", verification="", resolved_by="",
        confirm_resolved=False,
    )
    response = app.client.post("/api/tickets", json=body)
    assert response.status_code == 422
    fields = {detail["field"] for detail in response.json()["error"]["details"]}
    assert fields == {
        "subject", "category", "investigation", "resolution_steps", "verification", "resolved_by", "confirm_resolved",
    }
    assert app.client.post("/api/tickets", json=resolution(category="bad/category!")).status_code == 422
    assert app.client.post("/api/tickets", json=resolution(ticket_id="no spaces allowed")).status_code == 422
    assert app.client.post("/api/tickets", json=resolution(investigation="x" * 3001)).status_code == 422
    missing = {key: value for key, value in resolution().items() if key != "verification"}
    assert app.client.post("/api/tickets", json=missing).status_code == 422
    # Nothing was written anywhere.
    assert not (app.settings.data_dir / "saved_tickets.json").exists()
    assert app.settings.tickets_path.read_bytes() == seed_file


# --------------------------------------------------- save, then searchable
def test_saved_ticket_is_stored_and_found_by_a_later_search(app):
    assert app.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == []
    response = app.client.post("/api/tickets", json=resolution())
    assert response.status_code == 201
    saved = response.json()
    ticket_id = saved["ticket"]["ticket_id"]
    assert ticket_id.startswith("SR-")
    assert saved["outcome"] == "created" and saved["repeat_submission"] is False
    assert saved["indexing"]["status"] == "indexed" and saved["indexing"]["searchable"] is True
    assert "available for future searches" in saved["message"]
    assert saved["ticket"]["indexed"] is True and saved["ticket"]["index_status"] == "indexed"
    assert saved["ticket"]["resolution_notes"].startswith("Enabled schema version 3")
    assert saved["ticket"]["resolved_at"].endswith("Z")

    exact = app.search(NEW_SUBJECT, NEW_DESCRIPTION)
    assert exact["exact_match"]["matched_ticket_ids"] == [ticket_id]
    similar = app.search("Parquet bulk loader upload rejected", "Error LH-415 unsupported schema version on upload")
    assert ticket_id in _ids(similar)
    card = next(c for c in similar["results"] + similar["related"] if c["ticket"]["ticket_id"] == ticket_id)
    assert card["ticket"]["investigation"].startswith("Compared the failing files")
    assert card["ticket"]["verification"].startswith("All 12 re-run uploads")


def test_searching_alone_never_writes(app):
    for _ in range(3):
        app.search(NEW_SUBJECT, NEW_DESCRIPTION)
        app.search("Okta login loops back to the sign-in page", "Error 403.")
    assert not (app.settings.data_dir / "saved_tickets.json").exists()


# ------------------------------------------------------------- duplicates
def test_repeated_and_concurrent_submissions_create_one_record(app):
    body = resolution()
    first = app.client.post("/api/tickets", json=body).json()
    second = app.client.post("/api/tickets", json=body)
    assert second.status_code == 200
    assert second.json()["outcome"] == "unchanged" and second.json()["repeat_submission"] is True
    assert second.json()["ticket"]["ticket_id"] == first["ticket"]["ticket_id"]

    results: list[dict] = []
    other = resolution(subject="Bulk loader stalls at 99 percent")
    barrier = threading.Barrier(4)

    def post():
        barrier.wait()
        results.append(app.client.post("/api/tickets", json=other).json())

    threads = [threading.Thread(target=post) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({result["ticket"]["ticket_id"] for result in results}) == 1
    assert sorted(result["outcome"] for result in results) == ["created", "unchanged", "unchanged", "unchanged"]

    stored = json.loads((app.settings.data_dir / "saved_tickets.json").read_text(encoding="utf-8"))
    assert len(stored["tickets"]) == 2


def test_identical_text_from_a_different_incident_is_kept_separate(app):
    first = app.client.post("/api/tickets", json=resolution()).json()["ticket"]["ticket_id"]
    second = app.client.post("/api/tickets", json=resolution(resolution_steps="Asked the customer to re-export the files.")).json()
    assert second["ticket"]["ticket_id"] != first
    exact = app.search(NEW_SUBJECT, NEW_DESCRIPTION)
    assert exact["exact_match"]["matched_ticket_ids"] == [first, second["ticket"]["ticket_id"]]


# ------------------------------------------------------- stale updates
def test_existing_ticket_is_updated_only_with_the_current_revision(app):
    created = app.client.post("/api/tickets", json=resolution(ticket_id="INC-500")).json()["ticket"]
    revision = created["revision"]

    blind = app.client.post("/api/tickets", json=resolution(ticket_id="INC-500", verification="Different check."))
    assert blind.status_code == 409 and blind.json()["error"]["code"] == "ticket_conflict"

    updated = app.client.post(
        "/api/tickets", json=resolution(ticket_id="INC-500", verification="Re-checked on Monday.", expected_revision=revision)
    )
    assert updated.status_code == 200 and updated.json()["outcome"] == "updated"
    assert updated.json()["ticket"]["created_at"] == created["created_at"]

    stale = app.client.post(
        "/api/tickets", json=resolution(ticket_id="INC-500", verification="An older edit.", expected_revision=revision)
    )
    assert stale.status_code == 409
    stored = app.client.get("/api/tickets/INC-500").json()
    assert stored["verification"] == "Re-checked on Monday."

    # Seed tickets are protected the same way.
    seed = app.client.get("/api/tickets/A-1").json()
    assert app.client.post("/api/tickets", json=resolution(ticket_id="A-1")).status_code == 409
    replaced = app.client.post("/api/tickets", json=resolution(ticket_id="A-1", expected_revision=seed["revision"]))
    assert replaced.status_code == 200 and replaced.json()["outcome"] == "updated"
    assert app.settings.tickets_path.read_text(encoding="utf-8").count('"A-1"') == 1  # seed file untouched


# ------------------------------------------------------------- restart
def test_saved_tickets_survive_a_restart(tmp_path, sample_tickets_path):
    first = App(tmp_path, tickets_path=sample_tickets_path)
    ticket_id = first.client.post("/api/tickets", json=resolution()).json()["ticket"]["ticket_id"]
    first.close()

    second = App(tmp_path, tickets_path=sample_tickets_path)
    try:
        stored = second.client.get(f"/api/tickets/{ticket_id}").json()
        assert stored["indexed"] is True and stored["index_status"] == "indexed"
        assert stored["investigation"].startswith("Compared the failing files")
        assert second.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == [ticket_id]
    finally:
        second.close()


# ------------------------------------------------------ indexing failure
def test_indexing_failure_keeps_the_ticket_and_does_not_claim_success(app, tmp_path, sample_tickets_path):
    app.embedder.fail_on_encode = True
    response = app.client.post("/api/tickets", json=resolution())
    assert response.status_code == 201
    saved = response.json()
    ticket_id = saved["ticket"]["ticket_id"]
    assert saved["indexing"]["status"] == "failed" and saved["indexing"]["searchable"] is False
    assert "available for future searches" not in saved["message"]
    assert "not yet available" in saved["message"]

    stored = app.client.get(f"/api/tickets/{ticket_id}").json()
    assert stored["index_status"] == "failed" and stored["indexed"] is False
    assert stored["resolution_notes"].startswith("Enabled schema version 3")
    assert app.client.get("/api/meta").json()["unindexed_ticket_ids"] == [ticket_id]
    # Searches keep working from the previous snapshot, which does not have it yet.
    assert app.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == []

    app.embedder.fail_on_encode = False
    retried = app.client.post("/api/index/refresh").json()
    assert retried["indexing"]["status"] == "indexed" and retried["unindexed_ticket_ids"] == []
    assert app.client.get(f"/api/tickets/{ticket_id}").json()["indexed"] is True
    assert app.search(NEW_SUBJECT, NEW_DESCRIPTION)["exact_match"]["matched_ticket_ids"] == [ticket_id]


def test_pending_work_is_recovered_after_a_restart(tmp_path, sample_tickets_path):
    first = App(tmp_path, tickets_path=sample_tickets_path)
    first.embedder.fail_on_encode = True
    ticket_id = first.client.post("/api/tickets", json=resolution()).json()["ticket"]["ticket_id"]
    first.close()

    second = App(tmp_path, tickets_path=sample_tickets_path)
    try:
        assert second.client.get(f"/api/tickets/{ticket_id}").json()["index_status"] == "indexed"
        assert second.client.get("/api/meta").json()["unindexed_ticket_ids"] == []
    finally:
        second.close()


def test_unusable_store_refuses_saves_but_search_still_works(tmp_path, sample_tickets_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "saved_tickets.json").write_text("{ not json", encoding="utf-8")
    running = App(tmp_path, tickets_path=sample_tickets_path)
    try:
        assert running.client.get("/api/meta").json()["save_enabled"] is False
        refused = running.client.post("/api/tickets", json=resolution())
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "store_unavailable"
        assert (state / "saved_tickets.json").read_text(encoding="utf-8") == "{ not json"
        assert running.search("Okta login loops back to the sign-in page", "Error 403.")["status"] == "useful_matches"
    finally:
        running.close()


def test_saving_can_be_switched_off(tmp_path, sample_tickets_path):
    running = App(tmp_path, tickets_path=sample_tickets_path, save_enabled=False)
    try:
        response = running.client.post("/api/tickets", json=resolution())
        assert response.status_code == 403 and response.json()["error"]["code"] == "save_disabled"
    finally:
        running.close()


def test_identity_header_overrides_resolved_by_when_configured(tmp_path, sample_tickets_path):
    running = App(tmp_path, tickets_path=sample_tickets_path, identity_header="X-Forwarded-User")
    try:
        headers = {"X-Forwarded-User": "agent.ana@example.com"}
        assert running.client.get("/api/meta", headers=headers).json()["identity"] == "agent.ana@example.com"
        saved = running.client.post("/api/tickets", json=resolution(resolved_by=""), headers=headers).json()
        assert saved["ticket"]["resolved_by"] == "agent.ana@example.com"
    finally:
        running.close()
