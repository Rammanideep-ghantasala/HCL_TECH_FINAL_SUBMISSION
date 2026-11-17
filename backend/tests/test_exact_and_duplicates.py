"""Exact-match handling before similarity search, and duplicate prevention in the loader.

Searching must never add or change a ticket. Exact means the same subject and
the same description after only text.exact_text (line endings and surrounding
whitespace); no similarity score is ever treated as proof of identical text.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.engine import SearchInput
from app.main import create_app
from app.retrieval.semantic import build_semantic_index, historical_text, write_artifact
from app.tickets import load_corpus

from .conftest import REAL_TICKETS, SAMPLE_TICKETS, FakeEmbedder, build_engine, make_settings, make_ticket, write_tickets

NO_SCORES = {"keyword_cosine": None, "semantic_cosine": None, "query_coverage": None, "rrf": None}


def _cards(outcome: dict) -> list[dict]:
    return outcome["results"] + outcome["related"]


def _forbid_retrieval(engine, monkeypatch) -> None:
    """Make any keyword or semantic work fail loudly."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("similarity retrieval ran for an exact match")

    monkeypatch.setattr(engine.keyword, "query", refuse)
    monkeypatch.setattr(engine.semantic, "query", refuse)


# ----------------------------------------------------- searching never saves
def test_repeated_searches_leave_the_dataset_unchanged(tmp_path):
    tickets_path = tmp_path / "tickets.json"
    tickets_path.write_bytes(REAL_TICKETS.read_bytes())
    artifacts = tmp_path / "artifacts"
    write_artifact(load_corpus(tickets_path), FakeEmbedder(), artifacts)
    settings = make_settings(tmp_path, tickets_path, artifacts_dir=artifacts)

    def disk_state() -> dict:
        return {str(path.relative_to(tmp_path)): path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    before = disk_state()
    engine = build_engine(settings)
    records_before, version_before = engine.corpus.tickets, engine.index_version
    stored = engine.corpus.tickets[12]
    payloads = [
        {"subject": stored.subject, "description": stored.description},  # exact, no id
        {"subject": stored.subject, "description": stored.description, "ticket_id": stored.ticket_id},  # same id
        {"subject": "Changed subject", "description": "Changed text.", "ticket_id": stored.ticket_id},  # changed
        {"subject": stored.subject, "description": stored.description, "ticket_id": "NEW-1"},  # new id, same text
        {"subject": "Dashboard never loads", "description": "Spinner keeps going after refresh."},  # similar
        {"subject": "Zorblax plasma conduit", "description": "Vented xenon overnight.", "ticket_id": "NEW-2"},
    ]
    with TestClient(create_app(settings, engine)) as client:
        for _ in range(3):
            for payload in payloads:
                assert client.post("/api/search", json=payload).status_code == 200
        assert client.get("/api/meta").json()["corpus_size"] == 40
        for new_id in ("NEW-1", "NEW-2"):
            assert client.get(f"/api/tickets/{new_id}").status_code == 404

    assert disk_state() == before
    assert engine.corpus.tickets == records_before
    assert engine.index_version == version_before
    assert engine.corpus.by_id[stored.ticket_id] == stored


# ------------------------------------------------- exact match, without an id
def test_exact_text_without_an_id_returns_the_stored_evidence_and_skips_retrieval(real_engine, monkeypatch):
    stored = real_engine.corpus.tickets[17]
    embedder = real_engine.semantic.embedder
    encoded_before = len(embedder.encoded_texts)
    _forbid_retrieval(real_engine, monkeypatch)

    outcome = real_engine.search(SearchInput(subject=stored.subject, description=stored.description))

    assert outcome["status"] == "useful_matches"
    assert outcome["exact_match"] == {
        "ticket_id": None,
        "ticket_id_status": "not_supplied",
        "matched_ticket_ids": [stored.ticket_id],
        "message": None,
    }
    assert outcome["related"] == []
    assert outcome["retrieval"]["similarity_search_run"] is False
    assert outcome["timing_ms"]["keyword"] is None and outcome["timing_ms"]["semantic"] is None
    assert len(embedder.encoded_texts) == encoded_before, "no embedding should be computed"

    (card,) = outcome["results"]
    assert card["match_level"] == "exact" and card["match_label"] == "Exact text match"
    assert card["evidence"]["found_by"] == ["exact_text"]
    assert card["scores"] == NO_SCORES
    assert card["ticket"]["ticket_id"] == stored.ticket_id
    assert card["ticket"]["description"] == stored.description
    assert card["ticket"]["resolution_notes"] == stored.resolution_notes
    # It must not claim this is the same incident.
    assert "does not mean it is the same incident" in outcome["message"]
    assert "does not show that this is the same incident" in card["evidence"]["retrieval_note"]


def test_exact_normalization_is_limited_to_line_endings_and_surrounding_whitespace(tmp_path):
    record = make_ticket("N-1", "Sync fails at night", "Line one.\nLine two.", "Restarted the worker.")
    engine = build_engine(make_settings(tmp_path, write_tickets(tmp_path / "t.json", SAMPLE_TICKETS + [record])))

    def exact_ids(subject: str, description: str) -> list[str]:
        return engine.search(SearchInput(subject=subject, description=description))["exact_match"]["matched_ticket_ids"]

    # Normalized: surrounding whitespace and line endings only.
    assert exact_ids("  Sync fails at night \t", "\r\nLine one.\r\nLine two.\r\n") == ["N-1"]
    assert exact_ids("Sync fails at night", "Line one.\rLine two.") == ["N-1"]
    # Not normalized: case, punctuation, internal spacing, line breaks inside the text, negation.
    assert exact_ids("sync fails at night", "Line one.\nLine two.") == []
    assert exact_ids("Sync fails at night.", "Line one.\nLine two.") == []
    assert exact_ids("Sync  fails at night", "Line one.\nLine two.") == []
    assert exact_ids("Sync fails at night", "Line one. Line two.") == []
    assert exact_ids("Sync does not fail at night", "Line one.\nLine two.") == []
    # Fields are compared separately, so text cannot move between them.
    assert exact_ids("Sync fails at night\nLine one.", "Line two.") == []
    assert exact_ids("Sync fails at night", "") == []


# ------------------------------------------------ word order and scores of 1.0
def test_different_word_order_is_not_an_exact_match(real_engine):
    stored = next(t for t in real_engine.corpus.tickets if len(t.subject.split()) >= 4)
    reordered = " ".join(reversed(stored.subject.split()))
    outcome = real_engine.search(SearchInput(subject=reordered, description=stored.description))
    assert outcome["exact_match"]["matched_ticket_ids"] == []
    assert outcome["retrieval"]["similarity_search_run"] is True
    assert all(card["match_level"] != "exact" for card in _cards(outcome))


def test_a_keyword_cosine_of_one_is_not_treated_as_identical_text(real_engine):
    stored = real_engine.corpus.tickets[20]
    # Upper case and a doubled space disappear in keyword features, so TF-IDF sees the same text.
    restyled = stored.subject.upper().replace(" ", "  ", 1)
    outcome = real_engine.search(SearchInput(subject=restyled, description=stored.description))
    assert outcome["exact_match"]["matched_ticket_ids"] == []
    card = next(card for card in _cards(outcome) if card["ticket"]["ticket_id"] == stored.ticket_id)
    assert card["scores"]["keyword_cosine"] == pytest.approx(1.0)
    assert card["match_level"] != "exact"
    assert card["evidence"]["found_by"] != ["exact_text"]


# ------------------------------------------------------------- supplied ids
def test_supplied_id_with_the_same_text_reuses_the_existing_record(real_engine, monkeypatch):
    stored = real_engine.corpus.tickets[5]
    _forbid_retrieval(real_engine, monkeypatch)
    outcome = real_engine.search(
        SearchInput(subject=stored.subject, description=stored.description, ticket_id=f" {stored.ticket_id} ")
    )
    assert outcome["exact_match"]["ticket_id"] == stored.ticket_id
    assert outcome["exact_match"]["ticket_id_status"] == "exists_same_text"
    assert "Nothing was saved or changed" in outcome["exact_match"]["message"]
    first = outcome["results"][0]
    assert first["ticket"]["ticket_id"] == stored.ticket_id
    assert first["evidence"]["retrieval_note"].startswith(f"This is ticket {stored.ticket_id}, the id you supplied")
    assert real_engine.corpus.by_id[stored.ticket_id] == stored


def test_existing_id_with_changed_text_is_not_silently_overwritten(tmp_path):
    original = make_ticket("A-1", "Invoice total is wrong", "Old price after downgrade.", "Corrected the invoice.")
    changed = make_ticket("A-1", "Invoice total is wrong", "A different description.", "A different resolution.")

    # In the loader: the first record stays; the changed repeat is rejected and reported.
    path = write_tickets(tmp_path / "t.json", SAMPLE_TICKETS[1:] + [original, changed])
    file_before = path.read_bytes()
    corpus = load_corpus(path)
    assert corpus.ids.count("A-1") == 1
    assert corpus.by_id["A-1"].description == "Old price after downgrade."
    assert corpus.by_id["A-1"].resolution_notes == "Corrected the invoice."
    assert corpus.report.rejected["conflicting_duplicate_id"] == 1
    assert corpus.report.conflicting_ids == ["A-1"]

    # In a search: the id is recognised, the stored record is untouched, similarity search runs.
    engine = build_engine(make_settings(tmp_path, path))
    outcome = engine.search(
        SearchInput(subject="Invoice total is wrong", description="A different description.", ticket_id="A-1")
    )
    assert outcome["exact_match"]["ticket_id_status"] == "exists_different_text"
    assert "Nothing was saved or changed" in outcome["exact_match"]["message"]
    assert "no update workflow" in outcome["exact_match"]["message"]
    assert outcome["exact_match"]["matched_ticket_ids"] == []
    assert outcome["retrieval"]["similarity_search_run"] is True
    assert engine.corpus.by_id["A-1"].description == "Old price after downgrade."
    assert path.read_bytes() == file_before


def test_unknown_supplied_id_is_reported_and_not_added(sample_engine):
    outcome = sample_engine.search(
        SearchInput(subject="Okta login loops back to the sign-in page", description="Error 403 after sign-in.",
                    ticket_id="INC-77")
    )
    assert outcome["exact_match"]["ticket_id_status"] == "not_found"
    assert outcome["exact_match"]["message"] == "Ticket INC-77 is not in the history. Searching does not add it."
    assert "INC-77" not in sample_engine.corpus.by_id


# ---------------------------------------------- same text under different ids
def test_different_ids_with_identical_text_remain_separate_records(tmp_path):
    subject, description = "Export button missing", "The export button is not shown on the report page."
    first = make_ticket("B-1", subject, description, "Granted the export permission.", category="export")
    second = make_ticket("B-2", subject, description, "Cleared a stale feature flag.", category="perm")
    path = write_tickets(tmp_path / "t.json", SAMPLE_TICKETS + [first, second])

    corpus = load_corpus(path)
    assert {"B-1", "B-2"} <= set(corpus.ids)
    assert corpus.report.warnings["identical_text_different_id"] == 1
    assert corpus.report.rejected_total == 0

    engine = build_engine(make_settings(tmp_path, path))
    outcome = engine.search(SearchInput(subject=subject, description=description))
    assert outcome["exact_match"]["matched_ticket_ids"] == ["B-1", "B-2"]
    resolutions = {card["ticket"]["ticket_id"]: card["ticket"]["resolution_notes"] for card in outcome["results"]}
    assert resolutions == {"B-1": "Granted the export permission.", "B-2": "Cleared a stale feature flag."}
    assert len({card["result_id"] for card in outcome["results"]}) == 2
    assert "Each is a separate record" in outcome["message"]

    # The supplied id comes first; the other record is still returned.
    outcome = engine.search(SearchInput(subject=subject, description=description, ticket_id="B-2"))
    assert outcome["exact_match"]["matched_ticket_ids"] == ["B-2", "B-1"]


def test_category_filter_applies_to_exact_matches(sample_engine):
    stored = sample_engine.corpus.by_id["A-1"]  # billing
    outcome = sample_engine.search(
        SearchInput(subject=stored.subject, description=stored.description, category="export")
    )
    assert outcome["exact_match"]["matched_ticket_ids"] == []
    assert outcome["retrieval"]["hidden_by_filter"] >= 1
    assert outcome["retrieval"]["similarity_search_run"] is True
    assert all(card["ticket"]["category"] == "export" for card in _cards(outcome))
    assert any(s["id"] == "clear_category_filter" for s in outcome["query_insights"]["suggestions"])


# ---------------------------------------------------- no duplicates on import
def test_repeating_an_unchanged_ticket_creates_no_duplicate_and_recomputes_nothing(tmp_path):
    raw = json.loads(REAL_TICKETS.read_text(encoding="utf-8"))["tickets"]
    original_path = write_tickets(tmp_path / "original.json", raw)
    repeated_path = write_tickets(tmp_path / "repeated.json", raw + [dict(raw[0]), dict(raw[7]), dict(raw[0])])

    original, repeated = load_corpus(original_path), load_corpus(repeated_path)
    assert repeated.ids == original.ids
    assert repeated.report.warnings["identical_duplicate"] == 3
    assert repeated.report.rejected_total == 0
    assert repeated.content_sha256 == original.content_sha256

    artifacts = tmp_path / "artifacts"
    embedder = FakeEmbedder()
    write_artifact(original, embedder, artifacts)
    embedder.encoded_texts.clear()
    index = build_semantic_index(repeated, embedder, artifacts)
    assert index.source == "artifact" and embedder.encoded_texts == []

    engines = [
        build_engine(make_settings(tmp_path, path, artifacts_dir=artifacts)) for path in (original_path, repeated_path)
    ]
    assert engines[0].index_version == engines[1].index_version
    assert len(engines[1].corpus.tickets) == 40


def test_a_new_id_is_a_separate_case_and_only_new_text_is_embedded(tmp_path):
    raw = json.loads(REAL_TICKETS.read_text(encoding="utf-8"))["tickets"]
    artifacts = tmp_path / "artifacts"
    embedder = FakeEmbedder()
    write_artifact(load_corpus(write_tickets(tmp_path / "a.json", raw)), embedder, artifacts)

    same_text_new_id = {**raw[3], "ticket_id": "T-9001", "resolution_notes": "Handled as its own case."}
    new_text = make_ticket("T-9002", "Brand new problem", "Something nobody reported before.")
    corpus = load_corpus(write_tickets(tmp_path / "b.json", raw + [same_text_new_id, new_text]))
    assert len(corpus.tickets) == 42
    assert corpus.by_id["T-9001"].resolution_notes == "Handled as its own case."
    assert corpus.by_id[raw[3]["ticket_id"]].resolution_notes == raw[3]["resolution_notes"]

    embedder.encoded_texts.clear()
    index = build_semantic_index(corpus, embedder, artifacts)
    assert index.source == "artifact_partial"
    # The new id with already-embedded text reuses that vector; only the new text is computed.
    assert embedder.encoded_texts == [historical_text(corpus.by_id["T-9002"])]


# ------------------------------------------------ non-exact queries unchanged
def test_non_exact_queries_still_use_the_similarity_flow(sample_engine):
    embedder = sample_engine.semantic.embedder
    encoded_before = len(embedder.encoded_texts)
    outcome = sample_engine.search(
        SearchInput(
            subject="Invoice still shows the old price",
            description="We downgraded the plan last week but the invoice did not change.",
        )
    )
    assert outcome["exact_match"] == {
        "ticket_id": None, "ticket_id_status": "not_supplied", "matched_ticket_ids": [], "message": None,
    }
    assert outcome["retrieval"]["similarity_search_run"] is True
    assert len(embedder.encoded_texts) > encoded_before
    assert outcome["timing_ms"]["keyword"] is not None
    top = outcome["results"][0]
    assert top["ticket"]["ticket_id"] == "A-1"
    assert top["match_level"] in ("useful", "related")
    assert set(top["evidence"]["found_by"]) <= {"keyword", "semantic"}
    assert top["scores"]["keyword_cosine"] is not None


def test_vague_input_is_still_reported_as_insufficient(sample_engine):
    outcome = sample_engine.search(SearchInput(subject="help not working"))
    assert outcome["status"] == "insufficient_input"
    assert outcome["exact_match"]["matched_ticket_ids"] == []
    assert outcome["retrieval"]["similarity_search_run"] is False


# --------------------------------------------------------------------- API
def test_api_accepts_an_optional_ticket_id_and_validates_it(tmp_path, sample_tickets_path):
    settings = make_settings(tmp_path, sample_tickets_path)
    with TestClient(create_app(settings, build_engine(settings))) as client:
        stored = client.get("/api/tickets/A-2").json()
        exact = client.post(
            "/api/search",
            json={"subject": stored["subject"], "description": stored["description"], "ticket_id": "A-2"},
        )
        assert exact.status_code == 200
        body = exact.json()
        assert body["exact_match"]["ticket_id_status"] == "exists_same_text"
        assert body["results"][0]["match_level"] == "exact"
        assert body["results"][0]["ticket"]["resolution_notes"] == stored["resolution_notes"]

        # Requests without the new field behave as before.
        assert client.post("/api/search", json={"subject": "Okta login loops"}).status_code == 200

        bad = client.post("/api/search", json={"subject": "Okta login loops", "ticket_id": "not a valid id!"})
        assert bad.status_code == 422
        assert bad.json()["error"]["details"][0]["field"] == "ticket_id"
        assert "not a valid id!" not in bad.text
