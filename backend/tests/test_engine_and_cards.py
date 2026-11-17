from __future__ import annotations

import json
import random
import re

import pytest

from app.cards import MAX_DIFFERENCE_TERMS, MAX_QUESTIONS, MAX_SHARED_TERMS
from app.engine import SearchInput, SearchUnavailable, UnknownCategory
from app.signals import detect_codes, detect_names, find_referenced_ids
from app.tickets import load_corpus

from .conftest import (
    BACKEND_DIR,
    REAL_TICKETS,
    SAMPLE_TICKETS,
    FakeEmbedder,
    build_engine,
    make_settings,
    make_ticket,
    write_tickets,
)


def _all_cards(outcome: dict) -> list[dict]:
    return outcome["results"] + outcome["related"]


def _join(segments: list[dict]) -> str:
    return "".join(segment["text"] for segment in segments)


# ------------------------------------------------------------ source mapping
def test_every_card_shows_the_stored_record_for_its_ticket_id(real_engine):
    """Whatever is retrieved, the text shown must be that ticket's own text."""
    corpus = load_corpus(REAL_TICKETS)
    checked = 0
    for ticket in corpus.tickets:
        # Lower-cased so it is not an exact match and the full similarity flow runs.
        outcome = real_engine.search(SearchInput(subject=ticket.subject.lower(), description=ticket.description))
        assert outcome["retrieval"]["similarity_search_run"] is True
        for card in _all_cards(outcome):
            stored = corpus.by_id[card["ticket"]["ticket_id"]]
            assert card["ticket"]["resolution_notes"] == stored.resolution_notes
            assert card["ticket"]["subject"] == stored.subject
            assert card["ticket"]["description"] == stored.description
            assert card["ticket"]["tags"] == list(stored.tags)
            assert card["ticket"]["created_at"] == stored.created_at
            checked += 1
        assert outcome["results"], "a ticket's own text should find something"
        assert outcome["results"][0]["ticket"]["ticket_id"] == ticket.ticket_id
    assert checked > 40


def test_shuffling_the_file_does_not_change_which_resolution_belongs_to_which_id(tmp_path):
    raw = json.loads(REAL_TICKETS.read_text(encoding="utf-8"))["tickets"]
    shuffled = list(raw)
    random.Random(7).shuffle(shuffled)
    original = build_engine(make_settings(tmp_path, write_tickets(tmp_path / "a.json", raw)))
    reordered = build_engine(make_settings(tmp_path, write_tickets(tmp_path / "b.json", shuffled)))
    query = SearchInput(subject="Charged twice this month", description="Two charges on the card statement.")
    first = {c["ticket"]["ticket_id"]: c["ticket"]["resolution_notes"] for c in _all_cards(original.search(query))}
    second = {c["ticket"]["ticket_id"]: c["ticket"]["resolution_notes"] for c in _all_cards(reordered.search(query))}
    assert first and first == second


def test_same_request_twice_gives_the_same_answer(real_engine):
    query = SearchInput(subject="Dashboard never loads", description="Spinner keeps going after refresh.")
    first, second = real_engine.search(query), real_engine.search(query)
    first.pop("timing_ms"), second.pop("timing_ms")
    assert first == second
    assert first["results"] or first["related"]
    if first["results"]:
        assert first["results"][0]["result_id"].startswith(first["query_fingerprint"])


# --------------------------------------------------------- relevance outcomes
def test_results_are_never_padded_to_three(real_engine):
    outcome = real_engine.search(
        SearchInput(subject="Quantum flux capacitor misaligned", description="Zorblax plasma conduit vented xenon.")
    )
    assert outcome["status"] == "no_strong_match"
    assert outcome["results"] == []
    assert outcome["retrieval"]["keyword_signal"] is False
    assert set(outcome["query_insights"]["unseen_terms"]) & {"quantum", "zorblax", "plasma", "xenon"}


def test_at_most_three_useful_results(real_engine):
    for subject in ("Dashboard", "Customer login dashboard export billing sync api"):
        outcome = real_engine.search(SearchInput(subject=subject, description="dashboard chart export login sync"))
        assert len(outcome["results"]) <= 3
        assert len(outcome["related"]) <= 3


def test_vague_input_is_not_searched(sample_engine):
    embedder = sample_engine.semantic.embedder
    before = len(embedder.encoded_texts)
    outcome = sample_engine.search(SearchInput(subject="not working please help"))
    assert outcome["status"] == "insufficient_input"
    assert outcome["results"] == [] and outcome["related"] == []
    assert len(embedder.encoded_texts) == before, "no retrieval should run for vague input"
    assert any(s["field"] == "description" for s in outcome["query_insights"]["suggestions"])


def test_short_but_specific_input_is_searched(sample_engine):
    outcome = sample_engine.search(SearchInput(subject="Okta 403"))
    assert outcome["status"] != "insufficient_input"


# --------------------------------------------------------------- degradation
def test_model_failing_to_load_gives_keyword_only_mode(tmp_path, sample_tickets_path):
    engine = build_engine(make_settings(tmp_path, sample_tickets_path), broken_model=True)
    assert engine.ready and engine.mode == "keyword_only" and engine.degraded
    outcome = engine.search(SearchInput(subject="Invoice shows the old price after downgrade"))
    assert outcome["retrieval"]["mode"] == "keyword_only"
    assert outcome["retrieval"]["degraded"] is True
    assert outcome["retrieval"]["degraded_reason"] == "semantic_model_unavailable"
    assert outcome["results"][0]["ticket"]["ticket_id"] == "A-1"
    assert outcome["results"][0]["scores"]["semantic_cosine"] is None


def test_model_failing_during_a_request_falls_back_and_says_so(tmp_path, sample_tickets_path):
    embedder = FakeEmbedder()
    engine = build_engine(make_settings(tmp_path, sample_tickets_path), embedder=embedder)
    assert engine.mode == "hybrid"
    embedder.fail_on_encode = True
    outcome = engine.search(SearchInput(subject="Invoice shows the old price after downgrade"))
    assert outcome["retrieval"]["mode"] == "keyword_only"
    assert outcome["retrieval"]["degraded_reason"] == "semantic_error"
    assert outcome["results"][0]["ticket"]["ticket_id"] == "A-1"
    # After repeated failures the semantic retriever is switched off until restart.
    engine.search(SearchInput(subject="Okta login loops"))
    engine.search(SearchInput(subject="Okta login loops"))
    assert engine.mode == "keyword_only"
    assert engine.degraded_reason == "semantic_disabled_after_errors"


def test_semantic_disabled_by_configuration(tmp_path, sample_tickets_path):
    engine = build_engine(make_settings(tmp_path, sample_tickets_path, semantic_enabled=False))
    assert engine.mode == "keyword_only"
    assert engine.degraded_reason == "semantic_disabled_by_configuration"


def test_no_tickets_means_not_ready_and_search_is_an_error_not_a_no_match(tmp_path):
    engine = build_engine(make_settings(tmp_path, write_tickets(tmp_path / "empty.json", [])))
    assert not engine.ready and engine.mode == "unavailable"
    with pytest.raises(SearchUnavailable):
        engine.search(SearchInput(subject="Invoice total is wrong"))


def test_unreadable_ticket_file_means_not_ready(tmp_path):
    engine = build_engine(make_settings(tmp_path, tmp_path / "missing.json"))
    assert not engine.ready
    assert "ticket_file_unusable" in engine.problems


def test_missing_relevance_config_means_not_ready(tmp_path, sample_tickets_path):
    settings = make_settings(tmp_path, sample_tickets_path, relevance_config_path=tmp_path / "missing.json")
    assert not build_engine(settings).ready


# -------------------------------------------------------------------- filters
def test_category_filter_restricts_results_and_reports_what_it_hides(sample_engine):
    query = dict(subject="Invoice shows the old price after downgrade", description="Plan was downgraded last week.")
    unfiltered = sample_engine.search(SearchInput(**query))
    assert unfiltered["results"][0]["ticket"]["ticket_id"] == "A-1"
    filtered = sample_engine.search(SearchInput(**query, category="export"))
    assert all(card["ticket"]["category"] == "export" for card in _all_cards(filtered))
    assert filtered["retrieval"]["category_filter"] == "export"
    assert filtered["retrieval"]["hidden_by_filter"] >= 1
    assert any(s["id"] == "clear_category_filter" for s in filtered["query_insights"]["suggestions"])


def test_unknown_category_is_rejected(sample_engine):
    with pytest.raises(UnknownCategory):
        sample_engine.search(SearchInput(subject="Invoice total wrong", category="made-up"))


# ---------------------------------------------------------------------- cards
def _card(engine, **query) -> dict:
    outcome = engine.search(SearchInput(**query))
    cards = _all_cards(outcome)
    assert cards, "expected at least one card"
    return cards[0]


def test_highlight_segments_reproduce_both_tickets_exactly(sample_engine):
    subject = "Okta sign-in <script>alert(1)</script> loops"
    description = "After signing in with Okta the page returns to sign-in. Error 403 shown."
    card = _card(sample_engine, subject=subject, description=description)
    evidence = card["evidence"]
    assert _join(evidence["new_subject_segments"]) == subject
    assert _join(evidence["new_description_segments"]) == description
    assert _join(evidence["past_subject_segments"]) == card["ticket"]["subject"]
    assert _join(evidence["past_description_segments"]) == card["ticket"]["description"]
    for excerpt in evidence["excerpts"]:
        assert _join(excerpt["segments"]) in card["ticket"]["description"]


def test_card_word_lists_are_disjoint_capped_and_grounded(real_engine):
    corpus = load_corpus(REAL_TICKETS)
    for ticket in corpus.tickets[::5]:
        outcome = real_engine.search(SearchInput(subject=ticket.subject, description="It stopped working today."))
        for card in _all_cards(outcome):
            shared = set(card["evidence"]["shared_terms"])
            only_new = set(card["differences"]["only_in_new"])
            only_past = set(card["differences"]["only_in_past"])
            assert not (shared & only_new) and not (shared & only_past) and not (only_new & only_past)
            assert len(shared) <= MAX_SHARED_TERMS
            assert len(only_new) <= MAX_DIFFERENCE_TERMS and len(only_past) <= MAX_DIFFERENCE_TERMS
            assert len(card["verification_questions"]) <= MAX_QUESTIONS
            past_text = (card["ticket"]["subject"] + " " + card["ticket"]["description"]).lower()
            for word in only_past | shared:
                assert word in past_text


def test_unknown_is_never_reported_as_a_mismatch(real_engine):
    card = _card(real_engine, subject="Charged twice this month", description="Two charges on the statement.")
    for row in card["differences"]["comparison_rows"]:
        if not row["new_values"] or not row["past_values"]:
            assert row["outcome"] == "unknown"
            assert "different" not in row["statement"].lower()
    by_key = {row["key"]: row for row in card["differences"]["comparison_rows"]}
    assert by_key["category"]["new_values"] == [] and by_key["category"]["outcome"] == "unknown"


def test_different_product_names_are_stated_and_asked_about(sample_engine):
    card = _card(
        sample_engine,
        subject="Login loops back to the sign-in page",
        description="After signing in the browser returns to the sign-in page.",
        product="Auth0",
    )
    assert card["ticket"]["ticket_id"] == "A-2"
    row = next(r for r in card["differences"]["comparison_rows"] if r["key"] == "product_or_system")
    assert row["outcome"] == "different"
    assert row["new_values"] == ["Auth0"] and "Okta" in row["past_values"]
    assert row["new_basis"] == "entered by you"
    question = next(q for q in card["verification_questions"] if q["id"] == "names_differ")
    assert "Auth0" in question["question"] and "Okta" in question["question"]
    assert question["question"].rstrip().endswith("?")


def test_different_error_codes_are_stated(sample_engine):
    card = _card(
        sample_engine,
        subject="Okta login loops back to the sign-in page",
        description="The browser returns to the sign-in page.",
        error_detail="error 500",
    )
    row = next(r for r in card["differences"]["comparison_rows"] if r["key"] == "error_code")
    assert row["new_values"] == ["500"] and row["past_values"] == ["403"]
    assert row["outcome"] == "different"


def test_questions_quote_the_source_and_never_assert_a_cause(real_engine):
    corpus = load_corpus(REAL_TICKETS)
    banned = re.compile(r"\b(caused by|the cause is|will fix|should fix|you should|likely|probably)\b|%", re.I)
    for ticket in corpus.tickets[::4]:
        outcome = real_engine.search(SearchInput(subject=ticket.subject, description=ticket.description))
        for card in _all_cards(outcome):
            stored = corpus.by_id[card["ticket"]["ticket_id"]]
            for question in card["verification_questions"]:
                assert question["basis"]
                text = question["question"]
                if question["id"] == "resolution_statement":
                    quoted = text.split("“", 1)[1].split("”", 1)[0]
                    assert quoted in stored.resolution_notes
                    template = text.replace(quoted, "")
                else:
                    template = text
                assert "?" in text
                assert not banned.search(template), text
            assert not banned.search(card["evidence"]["retrieval_note"])
            assert "%" not in card["match_label"]


def test_ticket_text_containing_markup_is_returned_unchanged(tmp_path):
    payload = '<img src=x onerror=alert(1)> & "quotes" </div>'
    tickets = SAMPLE_TICKETS + [
        make_ticket("H-1", f"Widget shows {payload}", f"The widget renders {payload} on the page.", f"Escaped {payload}.")
    ]
    engine = build_engine(make_settings(tmp_path, write_tickets(tmp_path / "t.json", tickets)))
    card = _card(engine, subject="Widget shows odd markup", description="The widget renders strange text on the page.")
    assert card["ticket"]["ticket_id"] == "H-1"
    assert payload in card["ticket"]["subject"]
    assert card["ticket"]["resolution_notes"] == f"Escaped {payload}."
    assert _join(card["evidence"]["past_subject_segments"]) == f"Widget shows {payload}"


def test_references_to_other_known_tickets_are_found(sample_engine):
    card = _card(sample_engine, subject="Okta login loops back to the sign-in page", description="Error 403.")
    assert card["ticket"]["ticket_id"] == "A-2"
    assert card["referenced_ticket_ids"] == ["A-1"]


# -------------------------------------------------------------------- signals
def test_name_detection_uses_capitalisation_not_a_product_list():
    lexicon = {"okta": "Okta"}
    assert detect_names("Our integration with Snowflake is failing", lexicon) == ["Snowflake"]
    assert detect_names("Customer cannot sign in. Dashboard is blank.", lexicon) == []
    assert detect_names("okta login loops", lexicon) == ["Okta"]
    assert detect_names("HubSpot and SSO are both down", lexicon) == ["HubSpot", "SSO"]


def test_code_detection():
    assert detect_codes("Getting 429 rate-limit errors and ERR_CONN_RESET") == ["ERR_CONN_RESET", "429"]
    assert detect_codes("Takes 300 seconds to load") == []
    assert detect_codes("See A-12 for details", frozenset({"A-12"})) == []


def test_referenced_ids_only_match_known_ids():
    known = frozenset({"T-1", "T-2"})
    assert find_referenced_ids("Same as T-2. Not T-9.", known, exclude="T-1") == ["T-2"]


# --------------------------------------------------------------- no hardcoding
def test_application_code_contains_no_ticket_ids_from_the_dataset():
    """Behaviour must not be special-cased for particular tickets or examples."""
    corpus = load_corpus(REAL_TICKETS)
    known = set(corpus.ids) | {"NT-1", "NT-2", "NT-3", "NT-4"}
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(i) for i in sorted(known)) + r")\b")
    offenders = []
    for path in sorted((BACKEND_DIR / "app").rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []
