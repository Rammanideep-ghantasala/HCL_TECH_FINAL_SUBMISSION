from __future__ import annotations

import json

import numpy as np
import pytest

from app.config import MODEL_ID, MODEL_REVISION
from app.retrieval import relevance as rel
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.keyword import KeywordIndex, KeywordIndexError
from app.retrieval.semantic import (
    EMBEDDINGS_FILE,
    MANIFEST_FILE,
    SemanticError,
    SemanticIndex,
    SentenceTransformerEmbedder,
    build_semantic_index,
    historical_text,
    split_into_windows,
    write_artifact,
)
from app.tickets import load_corpus

from .conftest import BACKEND_DIR, REAL_MODEL_DIR, REAL_TICKETS, FakeEmbedder, make_ticket, write_tickets


# -------------------------------------------------------------------- keyword
@pytest.fixture(scope="module")
def real_corpus():
    return load_corpus(REAL_TICKETS)


@pytest.fixture(scope="module")
def real_keyword(real_corpus):
    return KeywordIndex(real_corpus.tickets, bigrams=True, stemming=False)


def test_every_ticket_retrieves_itself_first(real_corpus, real_keyword):
    """Row-to-ticket mapping: a ticket's own text must come back as that ticket."""
    for ticket in real_corpus.tickets:
        query = real_keyword.query(ticket.problem_text)
        top = real_keyword.top_hits(query, 1)[0]
        assert real_keyword.row_ids[top.row] == ticket.ticket_id
        assert top.score == pytest.approx(1.0, abs=1e-6)


def test_query_with_no_known_word_has_no_signal_and_no_candidates(real_keyword):
    query = real_keyword.query("zzqx wvvk plorf")
    assert query.has_signal is False
    assert not np.any(query.scores)
    assert real_keyword.top_hits(query, 10) == []
    assert set(query.unseen_problem_terms) == {"zzqx", "wvvk", "plorf"}


def test_zero_score_tickets_are_never_candidates(real_keyword):
    query = real_keyword.query("idempotency")
    hits = real_keyword.top_hits(query, 40)
    assert all(hit.score > 0 for hit in hits)
    assert len(hits) < 40


def test_coverage_counts_unseen_words_against_a_match(real_keyword):
    known = real_keyword.query("charged twice card statement")
    diluted = real_keyword.query("charged twice card statement zzqx wvvk plorf blarg")
    row = int(np.argmax(known.scores))
    # Cosine ignores words it has never seen; coverage does not.
    assert diluted.scores[row] == pytest.approx(known.scores[row])
    assert diluted.coverage[row] < known.coverage[row]


def test_empty_vocabulary_raises_a_clear_error(tmp_path):
    corpus = load_corpus(write_tickets(tmp_path / "t.json", [make_ticket("S-1", "the", "it is")]))
    with pytest.raises(KeywordIndexError):
        KeywordIndex(corpus.tickets, bigrams=True, stemming=False)


def test_allowed_rows_restrict_candidates(real_corpus, real_keyword):
    allowed = {row for row, ticket in enumerate(real_corpus.tickets) if ticket.category == "billing"}
    hits = real_keyword.top_hits(real_keyword.query("dashboard chart blank"), 10, allowed)
    assert all(hit.row in allowed for hit in hits)


# --------------------------------------------------------------------- fusion
def test_rrf_formula_and_order():
    fused = reciprocal_rank_fusion(["a", "b", "c"], ["b", "a", "d"], k=60)
    by_id = {item.ticket_id: item for item in fused}
    assert by_id["a"].rrf == pytest.approx(1 / 61 + 1 / 62)
    assert by_id["c"].rrf == pytest.approx(1 / 63)
    assert by_id["d"].keyword_rank is None and by_id["d"].semantic_rank == 3
    assert [item.ticket_id for item in fused] == ["a", "b", "c", "d"]


def test_rrf_deduplicates_and_is_deterministic():
    first = reciprocal_rank_fusion(["x", "y", "x"], ["y", "x"], k=60)
    second = reciprocal_rank_fusion(["x", "y", "x"], ["y", "x"], k=60)
    assert first == second
    assert sorted(item.ticket_id for item in first) == ["x", "y"]
    # x and y swap ranks, so their fused scores tie; the id decides.
    assert [item.ticket_id for item in first] == ["x", "y"]


def test_rrf_top_score_is_the_same_for_good_and_bad_lists():
    """A fused score cannot tell a strong top result from a weak one."""
    strong = reciprocal_rank_fusion(["good"], ["good"])[0].rrf
    weak = reciprocal_rank_fusion(["barely"], ["barely"])[0].rrf
    assert strong == weak


def test_rrf_handles_one_empty_list():
    fused = reciprocal_rank_fusion([], ["a", "b"])
    assert [item.ticket_id for item in fused] == ["a", "b"]
    assert reciprocal_rank_fusion([], []) == []


# ------------------------------------------------------------------ relevance
def _config(**overrides) -> rel.RelevanceConfig:
    payload = json.loads((BACKEND_DIR / "config" / "relevance.json").read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        payload[section][key] = value
    return rel.parse_relevance_config(payload)


def test_shipped_relevance_config_is_valid():
    config = rel.load_relevance_config(BACKEND_DIR / "config" / "relevance.json")
    assert config.version


def test_decision_uses_mode_specific_thresholds():
    config = _config(
        hybrid__useful_semantic_min=0.5,
        hybrid__useful_coverage_min=0.3,
        hybrid__useful_semantic_alone_min=0.8,
        hybrid__related_semantic_min=0.4,
        hybrid__related_keyword_min=0.2,
        keyword_only__useful_cosine_min=0.3,
        keyword_only__useful_coverage_min=0.3,
        keyword_only__related_cosine_min=0.15,
        semantic_only__useful_min=0.7,
        semantic_only__related_min=0.4,
    )
    assert config.decide(rel.HYBRID, rel.Evidence(0.4, 0.6, 0.4)) == rel.USEFUL
    assert config.decide(rel.HYBRID, rel.Evidence(0.4, 0.6, 0.1)) == rel.RELATED
    assert config.decide(rel.HYBRID, rel.Evidence(0.0, 0.85, 0.0)) == rel.USEFUL
    assert config.decide(rel.HYBRID, rel.Evidence(0.05, 0.2, 0.05)) == rel.DROPPED
    assert config.decide(rel.KEYWORD_ONLY, rel.Evidence(0.4, None, 0.4)) == rel.USEFUL
    assert config.decide(rel.KEYWORD_ONLY, rel.Evidence(0.4, None, 0.1)) == rel.RELATED
    assert config.decide(rel.KEYWORD_ONLY, rel.Evidence(0.1, None, 0.9)) == rel.DROPPED
    assert config.decide(rel.SEMANTIC_ONLY, rel.Evidence(None, 0.75, None)) == rel.USEFUL
    assert config.decide(rel.SEMANTIC_ONLY, rel.Evidence(None, 0.3, None)) == rel.DROPPED


def test_incomplete_or_invalid_relevance_config_is_rejected(tmp_path):
    with pytest.raises(rel.RelevanceConfigError):
        rel.parse_relevance_config({"version": "x", "hybrid": {}})
    with pytest.raises(rel.RelevanceConfigError):
        _config(hybrid__useful_semantic_min="high")
    with pytest.raises(rel.RelevanceConfigError):
        rel.load_relevance_config(tmp_path / "missing.json")


# ------------------------------------------------------------------- semantic
def test_long_input_is_split_so_no_word_is_dropped():
    embedder = FakeEmbedder(max_tokens=12)
    words = [f"word{i}" for i in range(60)]
    text = " ".join(words[:25]) + ". " + " ".join(words[25:]) + "."
    windows = split_into_windows(text, embedder, max_windows=50)
    assert len(windows) > 1
    assert all(embedder.count_tokens(window) <= embedder.max_tokens for window in windows)
    covered = " ".join(windows)
    for word in words:
        assert word in covered


def test_short_input_is_one_window():
    assert split_into_windows("short ticket text", FakeEmbedder(), max_windows=4) == ["short ticket text"]


def test_too_many_windows_is_an_explicit_error():
    embedder = FakeEmbedder(max_tokens=5)
    with pytest.raises(SemanticError):
        split_into_windows(" ".join(f"w{i}" for i in range(100)), embedder, max_windows=3)


def test_query_scores_use_the_best_window(real_corpus):
    embedder = FakeEmbedder(max_tokens=14)
    index = SemanticIndex(embedder, embedder.encode([t.problem_text for t in real_corpus.tickets]), real_corpus.ids, "test")
    target = real_corpus.tickets[7]
    filler = "Unrelated filler sentence about weather and lunch plans for the team today."
    scores, windows = index.query(f"{filler} {target.description}", max_windows=50)
    assert windows > 1
    assert real_corpus.ids[int(np.argmax(scores))] == target.ticket_id


def test_stored_embeddings_are_reused_only_for_unchanged_text_and_model(tmp_path, real_corpus):
    embedder = FakeEmbedder()
    write_artifact(real_corpus, embedder, tmp_path)
    embedder.encoded_texts.clear()
    index = build_semantic_index(real_corpus, embedder, tmp_path)
    assert index.source == "artifact" and index.computed_rows == 0
    assert embedder.encoded_texts == []

    # A different model revision must not reuse the stored vectors.
    other = FakeEmbedder()
    other.revision = "another-revision"
    assert build_semantic_index(real_corpus, other, tmp_path).source == "rebuilt_at_startup"

    # A ticket whose text no longer matches its stored vector is the only one recomputed.
    manifest = json.loads((tmp_path / MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["rows"][3]["text_sha256"] = "0" * 64
    (tmp_path / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    index = build_semantic_index(real_corpus, embedder, tmp_path)
    assert index.source == "artifact_partial" and index.computed_rows == 1
    assert embedder.encoded_texts == [historical_text(real_corpus.tickets[3])]

    # A manifest without per-ticket rows (the earlier format) is not trusted.
    del manifest["rows"]
    (tmp_path / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    assert build_semantic_index(real_corpus, embedder, tmp_path).source == "rebuilt_at_startup"


def test_corrupt_or_missing_artifact_is_rebuilt(tmp_path, real_corpus):
    embedder = FakeEmbedder()
    assert build_semantic_index(real_corpus, embedder, tmp_path).source == "rebuilt_at_startup"
    write_artifact(real_corpus, embedder, tmp_path)
    np.save(tmp_path / EMBEDDINGS_FILE, np.zeros((3, embedder.dimension), dtype=np.float32))
    index = build_semantic_index(real_corpus, embedder, tmp_path)
    assert index.source == "rebuilt_at_startup"
    assert index.matrix.shape[0] == len(real_corpus.tickets)


def test_row_count_mismatch_is_refused(real_corpus):
    embedder = FakeEmbedder()
    with pytest.raises(SemanticError):
        SemanticIndex(embedder, np.zeros((2, embedder.dimension), dtype=np.float32), real_corpus.ids, "test")


def test_missing_model_files_raise_semantic_error(tmp_path):
    with pytest.raises(SemanticError):
        SentenceTransformerEmbedder(tmp_path / "nothing-here", MODEL_ID, MODEL_REVISION)


# ------------------------------------------------ real model (skipped if absent)
@pytest.fixture(scope="module")
def real_embedder():
    return SentenceTransformerEmbedder(REAL_MODEL_DIR, MODEL_ID, MODEL_REVISION)


@pytest.mark.model
def test_real_model_reports_its_limits(real_embedder):
    assert real_embedder.dimension == 384
    assert real_embedder.max_tokens == 256
    vectors = real_embedder.encode(["Customer charged twice", "Dashboard is blank"])
    assert vectors.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)


@pytest.mark.model
def test_real_model_every_ticket_is_its_own_nearest_neighbour(real_embedder, real_corpus, tmp_path):
    index = build_semantic_index(real_corpus, real_embedder, tmp_path)
    for row, ticket in enumerate(real_corpus.tickets):
        scores, _ = index.query(f"{ticket.subject}. {ticket.description}", max_windows=4)
        assert int(np.argmax(scores)) == row


@pytest.mark.model
def test_real_model_long_input_is_windowed_not_truncated(real_embedder):
    sentence = "The customer reports that the scheduled export did not arrive this week. "
    text = sentence * 60
    assert real_embedder.count_tokens(text) > real_embedder.max_tokens
    windows = split_into_windows(text, real_embedder, max_windows=24)
    assert len(windows) > 1
    assert all(real_embedder.count_tokens(window) <= real_embedder.max_tokens for window in windows)


@pytest.mark.model
def test_shipped_artifact_matches_the_running_configuration(real_embedder, real_corpus):
    artifacts = BACKEND_DIR / "artifacts"
    if not (artifacts / MANIFEST_FILE).is_file():
        pytest.skip("embeddings have not been precomputed")
    assert build_semantic_index(real_corpus, real_embedder, artifacts).source == "artifact"
