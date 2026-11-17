"""Comparison cards: the evidence shown next to each retrieved ticket.

Every statement on a card is one of four things and nothing else:

1. stored text of the past ticket, unchanged;
2. an exact-word comparison between the new ticket and the past ticket;
3. a field one side does not have, shown as unknown;
4. a fixed sentence template filled with words or a quote from the tickets.

No language model is involved, so the card cannot invent a cause or a fix. It
also cannot understand meaning: it compares words, not problems. The wording
below is chosen to say only what an exact-word comparison supports.
"""

from __future__ import annotations

from dataclasses import dataclass

from .signals import detect_codes, detect_names, find_referenced_ids
from .text import NEGATION_TERM, highlight_segments, split_sentences, tokenize
from .tickets import TicketRecord

MAX_SHARED_TERMS = 8
MAX_DIFFERENCE_TERMS = 6
MAX_UNSEEN_TERMS = 5
MAX_QUESTIONS = 3
MAX_EXCERPTS = 2
MAX_QUOTE_CHARS = 280
# A word found in more than this share of past tickets is too common to count as evidence.
COMMON_TERM_DF_RATIO = 0.2

EXACT = "exact"
USEFUL_LABEL = "Close text match"
RELATED_LABEL = "Related context"
EXACT_LABEL = "Exact text match"
_LABELS = {EXACT: EXACT_LABEL, "useful": USEFUL_LABEL, "related": RELATED_LABEL}

EXACT_NOTE = (
    "Exact text match: the subject and description are character-for-character identical to this past "
    "ticket's, after normalizing line endings and trimming surrounding whitespace. It was found by direct "
    "comparison, not by a similarity score, and no similarity search was run. Identical wording does not "
    "show that this is the same incident or that it has the same cause."
)


@dataclass(frozen=True)
class QueryView:
    """The new ticket as the card builder sees it."""

    subject: str
    description: str
    product: str
    error_detail: str
    terms: frozenset[str]
    ordered_terms: tuple[str, ...]
    names: tuple[str, ...]
    names_basis: str
    codes: tuple[str, ...]
    codes_basis: str
    unseen_terms: tuple[str, ...]


@dataclass(frozen=True)
class CorpusView:
    """Corpus-wide facts the card builder needs."""

    doc_count: int
    doc_frequency: dict[str, int]
    name_lexicon: dict[str, str]
    known_ids: frozenset[str]
    stemming: bool

    def is_common(self, term: str) -> bool:
        return self.doc_frequency.get(term, 0) / max(self.doc_count, 1) > COMMON_TERM_DF_RATIO

    def rarity(self, term: str) -> int:
        return -self.doc_frequency.get(term, 0)


def _ordered_terms(text: str, stemming: bool) -> list[str]:
    ordered: list[str] = []
    for token in tokenize(text, stemming):
        if token.term not in ordered:
            ordered.append(token.term)
    return ordered


def _segments(text: str, terms: set[str], stemming: bool) -> list[dict]:
    return [{"text": piece, "match": matched} for piece, matched in highlight_segments(text, terms, stemming)]


def _join_words(words: list[str]) -> str:
    return ", ".join(words)


def _values_row(
    key: str,
    label: str,
    new_values: list[str],
    past_values: list[str],
    new_basis: str,
    past_basis: str,
    unknown_new: str,
    unknown_past: str,
    noun: str,
) -> dict:
    new_keys = {value.lower() for value in new_values}
    past_keys = {value.lower() for value in past_values}
    if not new_values or not past_values:
        outcome = "unknown"
        if not new_values and not past_values:
            statement = f"Neither ticket gives {noun}. Nothing to compare."
        elif not new_values:
            statement = unknown_new
        else:
            statement = unknown_past
    elif new_keys == past_keys:
        outcome = "same"
        statement = f"Both tickets give the same {noun}: {_join_words(new_values)}."
    elif new_keys & past_keys:
        outcome = "partly_shared"
        both = [value for value in new_values if value.lower() in past_keys]
        only_new = [value for value in new_values if value.lower() not in past_keys]
        only_past = [value for value in past_values if value.lower() not in new_keys]
        parts = [f"Both give: {_join_words(both)}."]
        if only_new:
            parts.append(f"Only the new ticket gives: {_join_words(only_new)}.")
        if only_past:
            parts.append(f"Only the past ticket gives: {_join_words(only_past)}.")
        statement = " ".join(parts)
    else:
        outcome = "different"
        statement = (
            f"The tickets give different {noun}. New ticket: {_join_words(new_values)}. "
            f"Past ticket: {_join_words(past_values)}."
        )
    return {
        "key": key,
        "label": label,
        "new_values": new_values,
        "past_values": past_values,
        "new_basis": new_basis if new_values else "not provided",
        "past_basis": past_basis if past_values else "not recorded",
        "outcome": outcome,
        "statement": statement,
    }


def _recorded_row(key: str, label: str, past_values: list[str], statement_known: str, statement_unknown: str) -> dict:
    """A field that only past tickets carry. It is never a mismatch."""
    return {
        "key": key,
        "label": label,
        "new_values": [],
        "past_values": past_values,
        "new_basis": "not provided",
        "past_basis": "recorded field" if past_values else "not recorded",
        "outcome": "unknown",
        "statement": statement_known if past_values else statement_unknown,
    }


def build_card(
    *,
    query: QueryView,
    ticket: TicketRecord,
    corpus: CorpusView,
    rank: int,
    match_level: str,
    result_id: str,
    found_by: list[str],
    keyword_rank: int | None,
    semantic_rank: int | None,
    scores: dict,
    same_ticket_id: bool = False,
) -> dict:
    stemming = corpus.stemming
    past_problem_terms = _ordered_terms(ticket.problem_text, stemming)
    past_problem_set = set(past_problem_terms)
    past_all_set = set(_ordered_terms(ticket.all_text, stemming))

    def is_evidence(term: str) -> bool:
        return term != NEGATION_TERM and not corpus.is_common(term)

    shared = [term for term in past_problem_terms if term in query.terms and is_evidence(term)]
    shared_sorted = sorted(shared, key=lambda term: (corpus.rarity(term) * -1, term))
    shared_set = set(shared)
    only_in_past = [term for term in past_problem_terms if term not in query.terms and is_evidence(term)]
    only_in_new = [term for term in query.ordered_terms if term not in past_all_set and is_evidence(term)]

    excerpts: list[dict] = []
    scored_sentences = []
    for order, sentence in enumerate(split_sentences(ticket.description)):
        hits = {token.term for token in tokenize(sentence, stemming)} & shared_set
        if hits:
            scored_sentences.append((-len(hits), order, sentence))
    for _, _, sentence in sorted(scored_sentences)[:MAX_EXCERPTS]:
        excerpts.append({"field": "description", "segments": _segments(sentence, shared_set, stemming)})

    if "exact_text" in found_by:
        note = EXACT_NOTE
        if same_ticket_id:
            note = f"This is ticket {ticket.ticket_id}, the id you supplied, as stored. " + note
    elif "keyword" in found_by and "semantic" in found_by:
        note = (
            "Found by keyword search and by meaning-based search. Highlighted words are exact words "
            "both tickets use; they do not explain the meaning-based ranking."
        )
    elif "keyword" in found_by:
        note = "Found by keyword search: the tickets use some of the same words."
    else:
        note = (
            "Found by meaning-based search. The embedding model gives no word-level reason, "
            "so highlighted words, if any, are not why it was found."
        )
    if not shared and "exact_text" not in found_by:
        note += " The two tickets have no distinctive word in common."

    past_names = detect_names(ticket.problem_text, corpus.name_lexicon)
    past_codes = detect_codes(ticket.problem_text, corpus.known_ids)
    name_row = _values_row(
        "product_or_system",
        "Products, systems or named terms",
        list(query.names),
        past_names,
        query.names_basis,
        "detected by capitalisation in the past ticket's text",
        "The new ticket names no product or system, so this cannot be compared. Unknown is not a mismatch.",
        "The past ticket names no product or system.",
        "names",
    )
    code_row = _values_row(
        "error_code",
        "Error codes",
        list(query.codes),
        past_codes,
        query.codes_basis,
        "detected in the past ticket's text",
        "The new ticket gives no error code, so this cannot be compared. Unknown is not a mismatch.",
        "The past ticket records no error code.",
        "error codes",
    )
    rows = [
        name_row,
        code_row,
        _recorded_row(
            "category",
            "Category",
            [ticket.category] if ticket.category else [],
            "The new ticket has no category, so this was not compared.",
            "Neither ticket has a category.",
        ),
        _recorded_row(
            "tags",
            "Tags",
            list(ticket.tags),
            "The new ticket has no tags, so these were not compared.",
            "Neither ticket has tags.",
        ),
        _recorded_row(
            "created_at",
            "Created",
            [ticket.created_at] if ticket.created_at else [],
            "Date the past ticket was created. The date it was resolved is not recorded.",
            "The past ticket's creation date is not recorded.",
        ),
        _recorded_row(
            "status",
            "Status",
            [ticket.status] if ticket.status else [],
            "Status as recorded. Whether the fix was later reviewed or replaced is not recorded.",
            "The past ticket's status is not recorded.",
        ),
    ]

    questions: list[dict] = []
    ticket_id = ticket.ticket_id
    if name_row["outcome"] in ("different", "partly_shared"):
        new_only = [value for value in name_row["new_values"] if value.lower() not in {v.lower() for v in past_names}]
        past_only = [value for value in past_names if value.lower() not in {v.lower() for v in name_row["new_values"]}]
        if new_only and past_only:
            questions.append(
                {
                    "id": "names_differ",
                    "question": (
                        f"The new ticket names {_join_words(new_only)}; {ticket_id} names {_join_words(past_only)}. "
                        f"Was the resolution in {ticket_id} specific to {_join_words(past_only)}?"
                    ),
                    "basis": f"Names found in both tickets ({query.names_basis}; {name_row['past_basis']}).",
                }
            )
    if code_row["outcome"] in ("different", "partly_shared"):
        questions.append(
            {
                "id": "codes_differ",
                "question": (
                    f"The new ticket gives {_join_words(list(query.codes))}; {ticket_id} gives "
                    f"{_join_words(past_codes)}. Is it the same error?"
                ),
                "basis": "Error codes found in both tickets.",
            }
        )
    sentences = split_sentences(ticket.resolution_notes)
    if sentences and len(sentences[0]) <= MAX_QUOTE_CHARS:
        questions.append(
            {
                "id": "resolution_statement",
                "question": (
                    f"{ticket_id}'s resolution notes begin: “{sentences[0]}” "
                    "Is there any sign of the same in the new ticket?"
                ),
                "basis": (
                    f"Quoted from {ticket_id}'s resolution notes. It was written for that ticket "
                    "and has not been checked against the new one."
                ),
            }
        )
    if only_in_past:
        questions.append(
            {
                "id": "past_only_words",
                "question": (
                    f"Words in {ticket_id} that the new ticket does not use: "
                    f"{_join_words(only_in_past[:MAX_DIFFERENCE_TERMS])}. The new ticket does not say either way. "
                    "Do any of these apply?"
                ),
                "basis": (
                    f"Exact words from {ticket_id}'s subject and description. "
                    "Absent means not stated, not different."
                ),
            }
        )

    return {
        "result_id": result_id,
        "rank": rank,
        "match_level": match_level,
        "match_label": _LABELS[match_level],
        "ticket": ticket_payload(ticket),
        "evidence": {
            "found_by": found_by,
            "keyword_rank": keyword_rank,
            "semantic_rank": semantic_rank,
            "shared_terms": shared_sorted[:MAX_SHARED_TERMS],
            "new_subject_segments": _segments(query.subject, shared_set, stemming),
            "new_description_segments": _segments(query.description, shared_set, stemming),
            "past_subject_segments": _segments(ticket.subject, shared_set, stemming),
            "past_description_segments": _segments(ticket.description, shared_set, stemming),
            "excerpts": excerpts,
            "retrieval_note": note,
        },
        "differences": {
            "only_in_new": only_in_new[:MAX_DIFFERENCE_TERMS],
            "only_in_past": only_in_past[:MAX_DIFFERENCE_TERMS],
            "unseen_in_history": list(query.unseen_terms[:MAX_UNSEEN_TERMS]),
            "comparison_rows": rows,
        },
        "verification_questions": questions[:MAX_QUESTIONS],
        "referenced_ticket_ids": find_referenced_ids(ticket.all_text, corpus.known_ids, ticket.ticket_id),
        "scores": scores,
    }


def ticket_payload(ticket: TicketRecord) -> dict:
    """A stored ticket exactly as loaded."""
    return {
        "ticket_id": ticket.ticket_id,
        "subject": ticket.subject,
        "description": ticket.description,
        "resolution_notes": ticket.resolution_notes,
        "category": ticket.category,
        "status": ticket.status,
        "tags": list(ticket.tags),
        "created_at": ticket.created_at,
        "product": ticket.product,
        "error_detail": ticket.error_detail,
        "investigation": ticket.investigation,
        "root_cause": ticket.root_cause,
        "verification": ticket.verification,
        "resolved_by": ticket.resolved_by,
        "resolved_at": ticket.resolved_at,
    }
