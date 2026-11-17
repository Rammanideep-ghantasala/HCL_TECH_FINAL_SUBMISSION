from __future__ import annotations

import json

import pytest

from app.text import analyze, highlight_segments, meaningful_terms, tokenize, unigram_terms
from app.tickets import TicketDataError, load_corpus, load_examples

from .conftest import REAL_EXAMPLES, REAL_TICKETS, SAMPLE_TICKETS, make_ticket, write_tickets


# ------------------------------------------------------------------ tokenizer
def test_negation_is_preserved_not_dropped():
    assert "not" in unigram_terms("Customer cannot log in")
    assert "not" in unigram_terms("The email never arrives")
    assert "not" in unigram_terms("Export hasn't finished")
    assert unigram_terms("Customer can log in") != unigram_terms("Customer cannot log in")


def test_contractions_leave_no_fragments():
    terms = unigram_terms("The customer's export doesn't start and they've retried")
    assert "customer" in terms
    for fragment in ("s", "t", "doesn", "ve", "hasn"):
        assert fragment not in terms


def test_codes_and_meaningful_numbers_are_kept_small_numbers_are_not():
    terms = unigram_terms("Got ERR_CONN_RESET and HTTP 429 after 15 minutes on 2FA")
    assert "err_conn_reset" in terms
    assert "429" in terms
    assert "2fa" in terms
    assert "15" not in terms


def test_phrases_only_join_words_that_are_adjacent_in_the_text():
    features = analyze("log in after password reset", bigrams=True)
    assert "password reset" in features
    assert "log password" not in features  # "in after" sat between them


def test_highlight_segments_reproduce_the_text_exactly():
    text = "Customer's card <b>statement</b> shows two charges — can't explain why."
    pieces = highlight_segments(text, {"statement", "charges", "not"})
    assert "".join(piece for piece, _ in pieces) == text
    assert [piece for piece, hit in pieces if hit] == ["statement", "charges", "can't"]


def test_tokens_carry_offsets_into_the_original_text():
    text = "Revenue CHART is Blank"
    for token in tokenize(text):
        assert text[token.start : token.end].lower().startswith(token.term[:3])


def test_vague_text_has_no_meaningful_terms():
    assert meaningful_terms("it is not working please help") == []
    assert len(meaningful_terms("2FA SMS")) == 2


# --------------------------------------------------------------------- loader
def test_supplied_dataset_loads_completely_and_is_not_modified():
    before = REAL_TICKETS.read_bytes()
    corpus = load_corpus(REAL_TICKETS)
    assert len(corpus.tickets) == 40
    assert len(set(corpus.ids)) == 40
    assert corpus.report.rejected_total == 0
    assert len(corpus.categories) == 8
    assert all(ticket.resolution_notes for ticket in corpus.tickets)
    assert REAL_TICKETS.read_bytes() == before


def test_supplied_examples_load():
    examples = load_examples(REAL_EXAMPLES)
    assert len(examples) == 4
    assert all(example.subject and example.description for example in examples)


def test_examples_missing_file_gives_empty_list(tmp_path):
    assert load_examples(tmp_path / "missing.json") == []


def test_malformed_records_are_rejected_with_reasons(tmp_path):
    good = make_ticket("G-1", "Subject one", "Description one")
    records = [
        good,
        "not an object",
        make_ticket("", "No id", "x"),
        {"ticket_id": "G-2", "description": "no subject", "resolution_notes": "r"},
        make_ticket("G-3", "No resolution", "x", resolution="   "),
        make_ticket("G-4", "Null description", None),
        make_ticket("G-5", "Still open", "x", status="open"),
        make_ticket("G-6", "Odd date", "x", created_at="yesterday"),
        make_ticket("G-7", "Tags not a list", "x", tags="oops"),
    ]
    corpus = load_corpus(write_tickets(tmp_path / "t.json", records))
    assert set(corpus.ids) == {"G-1", "G-6", "G-7"}
    rejected = corpus.report.rejected
    assert rejected["not_an_object"] == 1
    assert rejected["missing_or_invalid_id"] == 1
    assert rejected["missing_subject_or_description"] == 2
    assert rejected["missing_resolution"] == 1
    assert rejected["status_not_searchable"] == 1
    assert corpus.report.warnings["invalid_date"] == 1
    assert corpus.by_id["G-6"].created_at is None
    assert corpus.by_id["G-7"].tags == ()


def test_conflicting_duplicate_id_keeps_the_first_record_and_reports_the_rest(tmp_path):
    records = [
        make_ticket("D-1", "First version", "alpha", resolution="Resolution A"),
        make_ticket("D-1", "Second version", "beta", resolution="Resolution B"),
        make_ticket("D-2", "Unique", "gamma"),
        make_ticket("D-1", "Third version", "delta", resolution="Resolution C"),
    ]
    corpus = load_corpus(write_tickets(tmp_path / "t.json", records))
    assert corpus.ids == ("D-1", "D-2")
    assert corpus.by_id["D-1"].subject == "First version"
    assert corpus.by_id["D-1"].resolution_notes == "Resolution A"
    assert corpus.report.rejected["conflicting_duplicate_id"] == 2
    assert corpus.report.conflicting_ids == ["D-1"]


def test_identical_duplicates_collapse_to_one(tmp_path):
    record = make_ticket("D-1", "Same", "same text")
    corpus = load_corpus(write_tickets(tmp_path / "t.json", [record, dict(record)]))
    assert corpus.ids == ("D-1",)
    assert corpus.report.warnings["identical_duplicate"] == 1


@pytest.mark.parametrize(
    "content",
    ["not json at all", json.dumps([1, 2, 3]), json.dumps({"new_tickets": []}), json.dumps({"tickets": "nope"})],
)
def test_unusable_file_raises_instead_of_returning_nothing(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TicketDataError):
        load_corpus(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(TicketDataError):
        load_corpus(tmp_path / "missing.json")


def test_sample_fixture_is_valid(tmp_path):
    corpus = load_corpus(write_tickets(tmp_path / "t.json", SAMPLE_TICKETS))
    assert len(corpus.tickets) == len(SAMPLE_TICKETS)
