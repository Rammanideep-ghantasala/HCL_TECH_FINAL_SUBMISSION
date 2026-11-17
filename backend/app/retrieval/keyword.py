"""Keyword retrieval: TF-IDF vectors and cosine similarity.

The vocabulary and the IDF weights are fitted once on the historical tickets.
A new ticket is transformed with that same fitted vocabulary, so words the
history has never seen contribute nothing to the keyword score. That is why
this module also reports *coverage*: how much of the new ticket's wording a
past ticket actually accounts for, with never-seen words counted against it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from ..text import analyze, unigram_terms
from ..tickets import TicketRecord


class KeywordIndexError(Exception):
    """The keyword index could not be built (for example, an empty vocabulary)."""


@dataclass(frozen=True)
class KeywordHit:
    row: int
    score: float


@dataclass(frozen=True)
class KeywordQuery:
    """Everything the keyword side knows about one new ticket."""

    scores: np.ndarray  # cosine per index row; all zeros when there is no signal
    has_signal: bool  # False when the ticket shares no known term with the history
    coverage: np.ndarray  # per index row, 0..1
    query_terms: tuple[str, ...]  # distinct word terms of the new ticket
    unseen_problem_terms: tuple[str, ...]  # not in any past subject/description


class KeywordIndex:
    """Fitted TF-IDF matrix over each ticket's subject and description."""

    def __init__(self, tickets: tuple[TicketRecord, ...], *, bigrams: bool, stemming: bool) -> None:
        if not tickets:
            raise KeywordIndexError("no tickets to index")
        self.bigrams = bigrams
        self.stemming = stemming
        self.row_ids: tuple[str, ...] = tuple(ticket.ticket_id for ticket in tickets)
        self._vectorizer = TfidfVectorizer(
            analyzer=self._features,
            sublinear_tf=True,
            norm="l2",
            use_idf=True,
            smooth_idf=True,
        )
        texts = [ticket.problem_text for ticket in tickets]
        try:
            self._matrix = self._vectorizer.fit_transform(texts)
        except ValueError as exc:  # scikit-learn raises this for an empty vocabulary
            raise KeywordIndexError("the tickets contain no searchable words") from exc
        if self._matrix.shape[0] != len(self.row_ids):
            raise KeywordIndexError("index rows do not match the ticket list")

        # Word-level statistics for coverage, computed from word terms only.
        self._row_terms: list[frozenset[str]] = [
            frozenset(unigram_terms(text, self.stemming)) for text in texts
        ]
        doc_count = len(texts)
        doc_frequency: dict[str, int] = {}
        for terms in self._row_terms:
            for term in terms:
                doc_frequency[term] = doc_frequency.get(term, 0) + 1
        self.doc_frequency = doc_frequency
        self.doc_count = doc_count
        self._max_idf = self._idf(0)

    # -- helpers -----------------------------------------------------------
    def _features(self, text: str) -> list[str]:
        return analyze(text, stem=self.stemming, bigrams=self.bigrams)

    def _idf(self, doc_frequency: int) -> float:
        return math.log((1 + self.doc_count) / (1 + doc_frequency)) + 1.0

    def term_idf(self, term: str) -> float:
        return self._idf(self.doc_frequency.get(term, 0))

    @property
    def vocabulary_size(self) -> int:
        return len(self._vectorizer.vocabulary_)

    def describe(self) -> str:
        return (
            f"tfidf(words{'+phrases' if self.bigrams else ''},"
            f"stem={'on' if self.stemming else 'off'},sublinear_tf,l2,smooth_idf)"
        )

    def row_terms(self, row: int) -> frozenset[str]:
        return self._row_terms[row]

    # -- querying ----------------------------------------------------------
    def query(self, text: str) -> KeywordQuery:
        """Score one new ticket against every indexed ticket."""
        vector = self._vectorizer.transform([text])
        has_signal = vector.nnz > 0
        if has_signal:
            scores = np.asarray((self._matrix @ vector.T).todense()).ravel().astype(np.float64)
            scores = np.clip(scores, 0.0, 1.0)
        else:
            scores = np.zeros(len(self.row_ids), dtype=np.float64)

        distinct: list[str] = []
        for term in unigram_terms(text, self.stemming):
            if term not in distinct:
                distinct.append(term)
        weights = {term: self.term_idf(term) for term in distinct}
        total = sum(weights.values())
        coverage = np.zeros(len(self.row_ids), dtype=np.float64)
        if total > 0:
            for row, terms in enumerate(self._row_terms):
                covered = sum(weight for term, weight in weights.items() if term in terms)
                coverage[row] = covered / total
        unseen = tuple(term for term in distinct if term not in self.doc_frequency)
        return KeywordQuery(
            scores=scores,
            has_signal=has_signal,
            coverage=coverage,
            query_terms=tuple(distinct),
            unseen_problem_terms=unseen,
        )

    def top_hits(self, query: KeywordQuery, top_k: int, allowed_rows: set[int] | None = None) -> list[KeywordHit]:
        """Best rows by cosine. Rows scoring zero are never candidates."""
        if not query.has_signal:
            return []
        rows = [
            row
            for row in range(len(self.row_ids))
            if query.scores[row] > 0.0 and (allowed_rows is None or row in allowed_rows)
        ]
        rows.sort(key=lambda row: (-query.scores[row], self.row_ids[row]))
        return [KeywordHit(row=row, score=float(query.scores[row])) for row in rows[:top_k]]
