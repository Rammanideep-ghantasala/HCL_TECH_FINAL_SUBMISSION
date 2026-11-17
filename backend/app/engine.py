"""The search engine: one immutable snapshot of data and indexes, and the search flow.

Start-up builds everything once (tickets, keyword index, semantic index,
exact-text index, relevance thresholds). A request only reads that snapshot,
so no search can see a half-built index, and no search can add or change a
ticket.

Each search first checks for past tickets whose subject and description are
exactly the same text (see text.exact_text). Only when there is none does it
run keyword and semantic retrieval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cards
from .cards import CorpusView, QueryView
from .config import MODEL_ID, MODEL_REVISION, Settings
from .retrieval import relevance as rel
from .retrieval.fusion import reciprocal_rank_fusion
from .retrieval.keyword import KeywordIndex, KeywordIndexError, KeywordQuery
from .retrieval.semantic import (
    SEMANTIC_TEMPLATE_VERSION,
    Embedder,
    SemanticError,
    SemanticIndex,
    SentenceTransformerEmbedder,
    build_semantic_index,
    query_text,
    text_fingerprint,
)
from .repository import RepositoryError, TicketRepository, create_repository
from .signals import build_name_lexicon, detect_codes, detect_names
from .text import PREPROCESSING_VERSION, clean_text, exact_text, meaningful_terms, unigram_terms
from .tickets import TicketCorpus, TicketDataError, TicketRecord

logger = logging.getLogger("app.engine")

SEMANTIC_FAILURE_LIMIT = 3


class SearchUnavailable(Exception):
    """No retrieval method can answer. This is a service error, not "no match"."""


class UnknownCategory(Exception):
    pass


@dataclass(frozen=True)
class SearchInput:
    subject: str = ""
    description: str = ""
    product: str = ""
    error_detail: str = ""
    category: str | None = None
    # The new ticket's own id, when the caller has one. Used only to look up an
    # existing record; a search never stores it.
    ticket_id: str | None = None
    top_k: int = 3


def embedding_cache_dir(settings: Settings, source_id: str) -> Path:
    """Writable vector cache, one folder per ticket source (never shared between databases).

    Inside, vectors are keyed by the exact embedded text and the model's id,
    revision, size and text template, so a vector is reused only for
    identical text under an identical model and preprocessing configuration.
    """
    return settings.data_dir / "embeddings" / source_id


def _default_embedder(settings: Settings) -> Embedder:
    return SentenceTransformerEmbedder(
        settings.model_dir, MODEL_ID, MODEL_REVISION, num_threads=settings.torch_num_threads
    )


class SearchEngine:
    def __init__(
        self,
        settings: Settings,
        corpus: TicketCorpus | None,
        keyword: KeywordIndex | None,
        semantic: SemanticIndex | None,
        relevance: rel.RelevanceConfig | None,
        semantic_reason: str | None,
        problems: list[str],
    ) -> None:
        self.settings = settings
        self.corpus = corpus
        self.keyword = keyword
        self.semantic = semantic
        self.relevance = relevance
        self.semantic_reason = semantic_reason
        self.problems = problems
        self._semantic_failures = 0
        self._lock = threading.Lock()

        self._corpus_view: CorpusView | None = None
        self._all_terms: frozenset[str] = frozenset()
        self._rows_by_category: dict[str, set[int]] = {}
        # (subject, description) -> ids of every ticket with exactly that text, in file order.
        self._exact_index: dict[tuple[str, str], tuple[str, ...]] = {}
        if corpus is not None and corpus.tickets:
            exact: dict[tuple[str, str], list[str]] = {}
            for ticket in corpus.tickets:
                exact.setdefault(ticket.exact_key, []).append(ticket.ticket_id)
            self._exact_index = {key: tuple(ids) for key, ids in exact.items()}
            stemming = keyword.stemming if keyword else settings.keyword_stemming
            self._all_terms = frozenset(
                term for ticket in corpus.tickets for term in unigram_terms(ticket.all_text, stemming)
            )
            doc_frequency = keyword.doc_frequency if keyword else {}
            self._corpus_view = CorpusView(
                doc_count=len(corpus.tickets),
                doc_frequency=doc_frequency,
                name_lexicon=build_name_lexicon(
                    text for ticket in corpus.tickets for text in (ticket.subject, ticket.description)
                ),
                known_ids=frozenset(corpus.by_id),
                stemming=stemming,
            )
            for row, ticket in enumerate(corpus.tickets):
                if ticket.category:
                    self._rows_by_category.setdefault(ticket.category, set()).add(row)
        self.index_version = self._compute_index_version()

    # ------------------------------------------------------------------ build
    @classmethod
    def build(
        cls,
        settings: Settings,
        embedder_factory: Callable[[Settings], Embedder] | None = None,
        *,
        corpus: TicketCorpus | None = None,
        previous: "SearchEngine | None" = None,
        repository: "TicketRepository | None" = None,
    ) -> "SearchEngine":
        """Build a complete snapshot.

        With no `corpus`, every eligible ticket is loaded from the configured
        ticket repository (JSON or PostgreSQL). With `previous`, its loaded
        model and vectors are reused, so a rebuild after a save embeds only new
        or changed text and never reloads the model.
        """
        problems: list[str] = []
        if corpus is None:
            try:
                repo = repository or create_repository(settings)
                if not repo.writable:
                    problems.append("saved_store_unusable")
                corpus = repo.load_corpus(settings.searchable_status_set)
            except RepositoryError as exc:
                problems.append(exc.code)
                logger.error("ticket store unavailable", extra={"fields": {"reason": str(exc)}})
            except TicketDataError as exc:
                problems.append("ticket_file_unusable")
                logger.error("ticket file unusable", extra={"fields": {"reason": str(exc)}})
        if corpus is not None and not corpus.tickets:
            problems.append("no_valid_tickets")

        relevance: rel.RelevanceConfig | None = None
        try:
            relevance = rel.load_relevance_config(settings.relevance_config_path)
        except rel.RelevanceConfigError as exc:
            problems.append("relevance_config_unusable")
            logger.error("relevance config unusable", extra={"fields": {"reason": str(exc)}})

        keyword: KeywordIndex | None = None
        semantic: SemanticIndex | None = None
        semantic_reason: str | None = None
        if corpus is not None and corpus.tickets:
            if settings.keyword_enabled:
                try:
                    keyword = KeywordIndex(
                        corpus.tickets, bigrams=settings.keyword_bigrams, stemming=settings.keyword_stemming
                    )
                except KeywordIndexError as exc:
                    problems.append("keyword_index_unavailable")
                    logger.error("keyword index unavailable", extra={"fields": {"reason": str(exc)}})
            if not settings.semantic_enabled:
                semantic_reason = "semantic_disabled_by_configuration"
            elif previous is not None and previous.semantic is None and previous.semantic_reason is not None:
                # The model was unavailable before; a rebuild does not try to load it again.
                semantic_reason = previous.semantic_reason
            else:
                try:
                    if previous is not None and previous.semantic is not None:
                        embedder = previous.semantic.embedder
                    else:
                        embedder = (embedder_factory or _default_embedder)(settings)
                    semantic = build_semantic_index(
                        corpus,
                        embedder,
                        settings.artifacts_dir,
                        extra_dirs=(embedding_cache_dir(settings, corpus.source_id),),
                        known_vectors=previous.known_vectors() if previous is not None else None,
                    )
                    if semantic.row_ids != corpus.ids:
                        raise SemanticError("semantic rows do not match the ticket list")
                except SemanticError as exc:
                    semantic = None
                    semantic_reason = "semantic_model_unavailable"
                    logger.error("semantic retrieval unavailable", extra={"fields": {"reason": str(exc)}})
        return cls(settings, corpus, keyword, semantic, relevance, semantic_reason, problems)

    def _compute_index_version(self) -> str | None:
        if self.corpus is None:
            return None
        parts = {
            # Content of the accepted tickets, so a reordered file or a repeated
            # identical record does not change the version.
            "corpus": self.corpus.content_sha256,
            "preprocessing": PREPROCESSING_VERSION,
            "keyword": self.keyword.describe() if self.keyword else None,
            "semantic": (
                [self.semantic.embedder.model_id, self.semantic.embedder.revision, SEMANTIC_TEMPLATE_VERSION]
                if self.semantic
                else None
            ),
            "fusion": ["rrf", self.settings.rrf_k, self.settings.keyword_top_k, self.settings.semantic_top_k],
            "relevance": self.relevance.version if self.relevance else None,
        }
        digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()
        return digest[:12]

    def known_vectors(self) -> dict:
        """Text hash -> vector for every ticket in this snapshot's semantic index."""
        if self.semantic is None or self.corpus is None:
            return {}
        return {text_fingerprint(ticket): self.semantic.matrix[row] for row, ticket in enumerate(self.corpus.tickets)}

    def has_revision(self, ticket_id: str, revision: str) -> bool:
        """True when this snapshot holds exactly that version of the ticket."""
        record = self.corpus.by_id.get(ticket_id) if self.corpus else None
        return record is not None and record.revision == revision

    # ----------------------------------------------------------------- status
    @property
    def semantic_active(self) -> bool:
        return self.semantic is not None and self._semantic_failures < SEMANTIC_FAILURE_LIMIT

    @property
    def mode(self) -> str:
        if self.corpus is None or not self.corpus.tickets or self.relevance is None:
            return "unavailable"
        if self.keyword and self.semantic_active:
            return rel.HYBRID
        if self.keyword:
            return rel.KEYWORD_ONLY
        if self.semantic_active:
            return rel.SEMANTIC_ONLY
        return "unavailable"

    @property
    def ready(self) -> bool:
        return self.mode != "unavailable"

    @property
    def degraded(self) -> bool:
        return self.mode in (rel.KEYWORD_ONLY, rel.SEMANTIC_ONLY)

    @property
    def degraded_reason(self) -> str | None:
        if self.mode == rel.KEYWORD_ONLY:
            if self.semantic is not None:
                return "semantic_disabled_after_errors"
            return self.semantic_reason or "semantic_unavailable"
        if self.mode == rel.SEMANTIC_ONLY:
            return "keyword_index_unavailable"
        return None

    @property
    def model_id(self) -> str | None:
        return self.semantic.embedder.model_id if self.semantic else None

    @property
    def model_revision(self) -> str | None:
        return self.semantic.embedder.revision if self.semantic else None

    def categories(self) -> list[str]:
        return self.corpus.categories if self.corpus else []

    # ----------------------------------------------------------------- search
    def search(self, raw: SearchInput) -> dict:
        if not self.ready or self.corpus is None or self.relevance is None or self._corpus_view is None:
            raise SearchUnavailable("no retrieval capability is available")
        started = time.perf_counter()
        subject = clean_text(raw.subject).strip()
        description = clean_text(raw.description).strip()
        product = clean_text(raw.product).strip()
        error_detail = clean_text(raw.error_detail).strip()
        category = raw.category or None
        if category is not None and category not in self._rows_by_category:
            raise UnknownCategory(category)

        ticket_id = exact_text(raw.ticket_id or "") or None

        query_view = self._query_view(subject, description, product, error_detail)
        keyword_text = "\n".join(part for part in (subject, description, product, error_detail) if part)
        specific = meaningful_terms(keyword_text)
        fingerprint = self._fingerprint(subject, description, product, error_detail, category, ticket_id)

        # --- exact match first: plain string comparison, no tokens, scores or embeddings
        exact = self._exact_check(raw, ticket_id, category)
        base = {
            "query_fingerprint": fingerprint,
            "results": [],
            "related": [],
            "exact_match": exact["info"],
        }
        if exact["shown"]:
            return self._exact_response(
                base, exact, query_view, specific, category, fingerprint, started, raw.top_k
            )
        hidden_exact = exact["hidden"]

        if len(specific) < self.settings.min_meaningful_terms:
            insights = self._insights(
                query_view, specific, category, hidden=hidden_exact, searched=False, any_useful=False
            )
            return {
                **base,
                "status": "insufficient_input",
                "message": (
                    "There is not enough detail to search on. Describe what the customer did and what happened."
                ),
                "query_insights": insights,
                "retrieval": self._retrieval_info(
                    mode=self.mode,
                    degraded=self.degraded,
                    reason=self.degraded_reason,
                    keyword_signal=False,
                    category=category,
                    hidden=hidden_exact,
                    windows=0,
                    similarity_search_run=False,
                ),
                "timing_ms": {"total": _ms(started), "keyword": None, "semantic": None},
            }

        # --- keyword side
        keyword_query: KeywordQuery | None = None
        keyword_ms: float | None = None
        if self.keyword is not None:
            t0 = time.perf_counter()
            keyword_query = self.keyword.query(keyword_text)
            keyword_ms = _ms(t0)

        # --- semantic side
        semantic_scores = None
        semantic_ms: float | None = None
        windows = 0
        request_reason: str | None = None
        if self.semantic is not None and self.semantic_active:
            t0 = time.perf_counter()
            try:
                semantic_scores, windows = self.semantic.query(
                    query_text(subject, description, product, error_detail), self.settings.max_semantic_windows
                )
                with self._lock:
                    self._semantic_failures = 0
            except SemanticError as exc:
                with self._lock:
                    self._semantic_failures += 1
                request_reason = "semantic_error"
                logger.error("semantic query failed", extra={"fields": {"reason": str(exc)}})
            semantic_ms = _ms(t0)

        if keyword_query is not None and semantic_scores is not None:
            mode = rel.HYBRID
        elif keyword_query is not None:
            mode = rel.KEYWORD_ONLY
        elif semantic_scores is not None:
            mode = rel.SEMANTIC_ONLY
        else:
            raise SearchUnavailable("no retrieval method could answer this request")
        degraded = mode != rel.HYBRID
        reason = None
        if degraded:
            reason = request_reason or self.degraded_reason or "semantic_unavailable"

        allowed = self._rows_by_category[category] if category else None
        labelled = self.rank_and_label(mode, keyword_query, semantic_scores, allowed)
        hidden = hidden_exact
        if category:
            unfiltered = self.rank_and_label(mode, keyword_query, semantic_scores, None)
            shown = {item["ticket"].ticket_id for item in labelled if item["level"] == rel.USEFUL}
            hidden += sum(
                1
                for item in unfiltered[: raw.top_k * 2]
                if item["level"] == rel.USEFUL
                and item["ticket"].category != category
                and item["ticket"].ticket_id not in shown
            )

        useful = [item for item in labelled if item["level"] == rel.USEFUL][: raw.top_k]
        related = [item for item in labelled if item["level"] == rel.RELATED][: self.settings.max_related_results]

        def to_card(item: dict, rank: int) -> dict:
            ticket = item["ticket"]
            return cards.build_card(
                query=query_view,
                ticket=ticket,
                corpus=self._corpus_view,
                rank=rank,
                match_level=item["level"],
                result_id=f"{fingerprint}:{ticket.ticket_id}:{self.index_version}",
                found_by=item["found_by"],
                keyword_rank=item["keyword_rank"],
                semantic_rank=item["semantic_rank"],
                scores=item["scores"],
            )

        result_cards = [to_card(item, rank) for rank, item in enumerate(useful, start=1)]
        related_cards = [to_card(item, rank) for rank, item in enumerate(related, start=1)]

        count = len(self.corpus.tickets)
        if result_cards:
            status = "useful_matches"
            noun = "past ticket looks" if len(result_cards) == 1 else "past tickets look"
            message = (
                f"{len(result_cards)} {noun} like a close match. Check the differences before reusing a resolution."
            )
        elif related_cards:
            status = "no_strong_match"
            message = (
                "No past ticket is a strong match for this ticket. The tickets below share some context but are "
                "unlikely to hold the answer. Treat this as a new problem unless the evidence says otherwise."
            )
        else:
            status = "no_strong_match"
            message = f"Nothing similar was found in the {count} past tickets. Treat this as a new problem."

        return {
            **base,
            "status": status,
            "message": message,
            "results": result_cards,
            "related": related_cards,
            "query_insights": self._insights(
                query_view, specific, category, hidden=hidden, searched=True, any_useful=bool(result_cards)
            ),
            "retrieval": self._retrieval_info(
                mode=mode,
                degraded=degraded,
                reason=reason,
                keyword_signal=bool(keyword_query and keyword_query.has_signal),
                category=category,
                hidden=hidden,
                windows=windows,
            ),
            "timing_ms": {"total": _ms(started), "keyword": keyword_ms, "semantic": semantic_ms},
        }

    # ------------------------------------------------------------ exact match
    def _exact_check(self, raw: SearchInput, ticket_id: str | None, category: str | None) -> dict:
        """Past tickets whose subject and description are exactly the submitted text.

        Fields are compared separately, after text.exact_text and nothing else.
        A supplied ticket id is looked up but never stored or updated.
        """
        assert self.corpus is not None
        key = (exact_text(raw.subject), exact_text(raw.description))
        matches: list[TicketRecord] = [self.corpus.by_id[i] for i in self._exact_index.get(key, ())]
        status = "not_supplied"
        message: str | None = None
        if ticket_id is not None:
            existing = self.corpus.by_id.get(ticket_id)
            if existing is None:
                status = "not_found"
                message = f"Ticket {ticket_id} is not in the history. Searching does not add it."
            elif existing.exact_key == key:
                status = "exists_same_text"
                matches = [existing] + [m for m in matches if m.ticket_id != ticket_id]
                message = (
                    f"Ticket {ticket_id} is already in the history with exactly this subject and description. "
                    "Nothing was saved or changed."
                )
            else:
                status = "exists_different_text"
                message = (
                    f"Ticket {ticket_id} already exists with a different subject or description. Nothing was saved "
                    "or changed: a search never adds or updates tickets, and this service has no update workflow. "
                    f"The stored version of {ticket_id} is unchanged."
                )
        shown = [m for m in matches if category is None or m.category == category]
        return {
            "shown": shown,
            "hidden": len(matches) - len(shown),
            "info": {
                "ticket_id": ticket_id,
                "ticket_id_status": status,
                "matched_ticket_ids": [m.ticket_id for m in shown],
                "message": message,
            },
        }

    def _exact_response(
        self, base: dict, exact: dict, query_view: QueryView, specific: list[str], category: str | None,
        fingerprint: str, started: float, top_k: int,
    ) -> dict:
        """Answer from the exact matches alone. No keyword or semantic work is done."""
        assert self._corpus_view is not None
        shown: list[TicketRecord] = exact["shown"]
        same_id = exact["info"]["ticket_id_status"] == "exists_same_text"
        result_cards = [
            cards.build_card(
                query=query_view,
                ticket=ticket,
                corpus=self._corpus_view,
                rank=rank,
                match_level=cards.EXACT,
                result_id=f"{fingerprint}:{ticket.ticket_id}:{self.index_version}",
                found_by=["exact_text"],
                keyword_rank=None,
                semantic_rank=None,
                scores={"keyword_cosine": None, "semantic_cosine": None, "query_coverage": None, "rrf": None},
                same_ticket_id=same_id and rank == 1,
            )
            for rank, ticket in enumerate(shown[:top_k], start=1)
        ]
        ids = [ticket.ticket_id for ticket in shown]
        if len(ids) == 1:
            message = f"Exact text match found: the subject and description are identical to past ticket {ids[0]}."
        else:
            message = (
                f"Exact text match found: the subject and description are identical to {len(ids)} past tickets "
                f"({', '.join(ids)}). Each is a separate record."
            )
            if len(ids) > len(result_cards):
                message += f" The first {len(result_cards)} are shown."
        message += (
            " Identical wording does not mean it is the same incident or has the same cause. "
            "Check before reusing a resolution."
        )
        hidden = exact["hidden"]
        insights = self._insights(query_view, specific, category, hidden=hidden, searched=False, any_useful=True)
        # Refinement hints are for similarity search, which did not run.
        insights["suggestions"] = [s for s in insights["suggestions"] if s["id"] == "clear_category_filter"]
        return {
            **base,
            "status": "useful_matches",
            "message": message,
            "results": result_cards,
            "related": [],
            "query_insights": insights,
            "retrieval": self._retrieval_info(
                mode=self.mode,
                degraded=False,
                reason=None,
                keyword_signal=False,
                category=category,
                hidden=hidden,
                windows=0,
                similarity_search_run=False,
            ),
            "timing_ms": {"total": _ms(started), "keyword": None, "semantic": None},
        }

    # --------------------------------------------------------------- internals
    def rank_and_label(
        self,
        mode: str,
        keyword_query,
        semantic_scores,
        allowed: set[int] | None,
        relevance: rel.RelevanceConfig | None = None,
    ) -> list[dict]:
        """Fused order of candidates, each with its evidence and relevance level.

        The `relevance` argument overrides the loaded thresholds; the evaluation
        uses it to compare threshold settings without rebuilding anything.
        """
        assert self.corpus is not None and self.relevance is not None
        thresholds = relevance or self.relevance
        row_of = {ticket_id: row for row, ticket_id in enumerate(self.corpus.ids)}
        keyword_ids: list[str] = []
        if keyword_query is not None and self.keyword is not None:
            keyword_ids = [
                self.keyword.row_ids[hit.row]
                for hit in self.keyword.top_hits(keyword_query, self.settings.keyword_top_k, allowed)
            ]
        semantic_ids: list[str] = []
        if semantic_scores is not None and self.semantic is not None:
            semantic_ids = [
                self.semantic.row_ids[hit.row]
                for hit in self.semantic.top_hits(semantic_scores, self.settings.semantic_top_k, allowed)
            ]
        labelled: list[dict] = []
        for candidate in reciprocal_rank_fusion(keyword_ids, semantic_ids, self.settings.rrf_k):
            row = row_of[candidate.ticket_id]
            keyword_cosine = float(keyword_query.scores[row]) if keyword_query is not None else None
            coverage = float(keyword_query.coverage[row]) if keyword_query is not None else None
            semantic_cosine = float(semantic_scores[row]) if semantic_scores is not None else None
            level = thresholds.decide(mode, rel.Evidence(keyword_cosine, semantic_cosine, coverage))
            if level == rel.DROPPED:
                continue
            found_by = []
            if candidate.keyword_rank:
                found_by.append("keyword")
            if candidate.semantic_rank:
                found_by.append("semantic")
            labelled.append(
                {
                    "ticket": self.corpus.by_id[candidate.ticket_id],
                    "level": level,
                    "found_by": found_by,
                    "keyword_rank": candidate.keyword_rank,
                    "semantic_rank": candidate.semantic_rank,
                    "scores": {
                        "keyword_cosine": _round(keyword_cosine),
                        "semantic_cosine": _round(semantic_cosine),
                        "query_coverage": _round(coverage),
                        "rrf": _round(candidate.rrf),
                    },
                }
            )
        return labelled

    def _query_view(self, subject: str, description: str, product: str, error_detail: str) -> QueryView:
        assert self._corpus_view is not None
        stemming = self._corpus_view.stemming
        body = "\n".join(part for part in (subject, description) if part)
        everything = "\n".join(part for part in (subject, description, product, error_detail) if part)
        ordered: list[str] = []
        for term in unigram_terms(everything, stemming):
            if term not in ordered:
                ordered.append(term)

        if product:
            names, names_basis = (product,), "entered by you"
        else:
            names = tuple(detect_names(body, self._corpus_view.name_lexicon))
            names_basis = "detected by capitalisation in the new ticket's text"
        if error_detail:
            found = detect_codes(error_detail, self._corpus_view.known_ids)
            codes, codes_basis = (tuple(found) if found else (error_detail,)), "entered by you"
        else:
            codes = tuple(detect_codes(body, self._corpus_view.known_ids))
            codes_basis = "detected in the new ticket's text"

        unseen = tuple(
            term
            for term in ordered
            if term not in self._all_terms and not any(ch.isdigit() for ch in term) and "@" not in term
        )
        return QueryView(
            subject=subject,
            description=description,
            product=product,
            error_detail=error_detail,
            terms=frozenset(ordered),
            ordered_terms=tuple(ordered),
            names=names,
            names_basis=names_basis,
            codes=codes,
            codes_basis=codes_basis,
            unseen_terms=unseen,
        )

    def _insights(
        self, query: QueryView, specific: list[str], category: str | None, hidden: int, searched: bool, any_useful: bool
    ) -> dict:
        count = len(self.corpus.tickets) if self.corpus else 0
        suggestions: list[dict] = []
        if len(specific) < 5:
            word = "word" if len(specific) == 1 else "words"
            suggestions.append(
                {
                    "id": "add_description",
                    "field": "description",
                    "message": "Describe what the customer did and what happened instead.",
                    "basis": f"The ticket contains {len(specific)} specific {word}.",
                }
            )
        if not query.names:
            suggestions.append(
                {
                    "id": "add_product",
                    "field": "product",
                    "message": "Name the affected product, integration or feature if you know it.",
                    "basis": "No product or system name was entered or detected in the ticket text.",
                }
            )
        if not query.codes:
            suggestions.append(
                {
                    "id": "add_error_detail",
                    "field": "error_detail",
                    "message": "Add the exact error code or message if the customer saw one.",
                    "basis": "No error code or message was entered or detected in the ticket text.",
                }
            )
        unseen = list(query.unseen_terms[: cards.MAX_UNSEEN_TERMS])
        if searched and unseen:
            suggestions.append(
                {
                    "id": "check_unseen_terms",
                    "field": None,
                    "message": (
                        f"These words appear in no past ticket: {', '.join(unseen)}. If one is misspelt, "
                        "correct it. Otherwise this may be a kind of problem the history does not contain."
                    ),
                    "basis": f"Checked against every word in the {count} past tickets (exact words only).",
                }
            )
        if category and (hidden > 0 or not any_useful):
            suggestions.append(
                {
                    "id": "clear_category_filter",
                    "field": "category",
                    "message": (
                        "A category filter is active and can hide relevant tickets filed under another category. "
                        "Clear it to search every ticket."
                    ),
                    "basis": f"Filter: {category}. Close matches hidden in other categories: {hidden}.",
                }
            )
        return {
            "meaningful_term_count": len(specific),
            "detected_names": list(query.names),
            "detected_codes": list(query.codes),
            "unseen_terms": unseen,
            "suggestions": suggestions,
        }

    def _retrieval_info(
        self, *, mode: str, degraded: bool, reason: str | None, keyword_signal: bool, category: str | None,
        hidden: int, windows: int, similarity_search_run: bool = True,
    ) -> dict:
        assert self.relevance is not None and self.corpus is not None
        return {
            "similarity_search_run": similarity_search_run,
            "mode": mode,
            "degraded": degraded,
            "degraded_reason": reason,
            "keyword_signal": keyword_signal,
            "category_filter": category,
            "hidden_by_filter": hidden,
            "semantic_windows": windows,
            "corpus_size": len(self.corpus.tickets),
            "index_version": self.index_version,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "relevance_config_version": self.relevance.version,
        }

    @staticmethod
    def _fingerprint(
        subject: str, description: str, product: str, error_detail: str, category: str | None,
        ticket_id: str | None = None,
    ) -> str:
        normalized = [" ".join(part.lower().split()) for part in (subject, description, product, error_detail)]
        parts = normalized + [category or ""]
        if ticket_id:  # appended only when present, so earlier fingerprints are unchanged
            parts.append(ticket_id)
        payload = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 2)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)
