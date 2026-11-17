"""Semantic retrieval: sentence embeddings compared with cosine similarity.

Historical embeddings are computed once and stored with a manifest. The
manifest records exactly what produced them, and is checked at start-up, so a
query is never compared against vectors from a different model, a different
ticket file or a different text recipe.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ..text import split_sentences
from ..tickets import TicketCorpus, TicketRecord

logger = logging.getLogger("app.semantic")

SEMANTIC_TEMPLATE_VERSION = "sem-v1"
EMBEDDINGS_FILE = "embeddings.npy"
MANIFEST_FILE = "manifest.json"


class SemanticError(Exception):
    """The semantic retriever cannot answer (model missing, failed, or input unusable)."""


class Embedder(Protocol):
    model_id: str
    revision: str | None
    dimension: int
    max_tokens: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return one unit-length float32 vector per text."""

    def count_tokens(self, text: str) -> int:
        """Number of model tokens, including the special start and end tokens."""


class SentenceTransformerEmbedder:
    """The real model, loaded once from files already on disk."""

    def __init__(self, model_dir: Path, model_id: str, revision: str | None, num_threads: int = 1) -> None:
        if not (model_dir / "modules.json").is_file():
            raise SemanticError("embedding model files are not present")
        try:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(num_threads)
            self._model = SentenceTransformer(str(model_dir), device="cpu", local_files_only=True)
        except Exception as exc:  # any load failure means "semantic unavailable"
            raise SemanticError(f"embedding model failed to load: {type(exc).__name__}") from exc
        self.model_id = model_id
        self.revision = revision
        self.max_tokens = int(self._model.max_seq_length)
        self.dimension = int(self.encode(["warm up"]).shape[1])

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._model.encode(
            texts,
            batch_size=16,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def count_tokens(self, text: str) -> int:
        encoded = self._model.tokenizer(text, add_special_tokens=True, truncation=False)
        return len(encoded["input_ids"])


def historical_text(ticket: TicketRecord) -> str:
    """The text embedded for a past ticket: what the customer reported."""
    return f"{ticket.subject}. {ticket.description}"


def query_text(subject: str, description: str, product: str, error_detail: str) -> str:
    """The text embedded for a new ticket. Built the same way as historical_text."""
    parts = [part for part in (subject.strip(), description.strip()) if part]
    text = ". ".join(part.rstrip(".") for part in parts)
    if text:
        text += "."
    if product.strip():
        text += f" Product or system: {product.strip()}."
    if error_detail.strip():
        text += f" Error: {error_detail.strip()}."
    return text.strip()


def split_into_windows(text: str, embedder: Embedder, max_windows: int) -> list[str]:
    """Cover the whole text with pieces that each fit the model's input limit.

    The model silently cuts off anything beyond its limit, so long input is
    split on sentence boundaries (and inside a sentence only when one sentence
    alone is too long). Every word ends up in some window.
    """
    limit = embedder.max_tokens
    if embedder.count_tokens(text) <= limit:
        return [text]

    pieces: list[str] = []
    for sentence in split_sentences(text) or [text]:
        if embedder.count_tokens(sentence) <= limit:
            pieces.append(sentence)
            continue
        words = sentence.split()
        current: list[str] = []
        for word in words:
            candidate = " ".join(current + [word])
            if current and embedder.count_tokens(candidate) > limit:
                pieces.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            pieces.append(" ".join(current))

    windows: list[str] = []
    current_window = ""
    for piece in pieces:
        candidate = f"{current_window} {piece}".strip()
        if current_window and embedder.count_tokens(candidate) > limit:
            windows.append(current_window)
            current_window = piece
        else:
            current_window = candidate
    if current_window:
        windows.append(current_window)

    if len(windows) > max_windows:
        raise SemanticError("input needs more model windows than the configured budget")
    return windows


@dataclass(frozen=True)
class SemanticHit:
    row: int
    score: float


class SemanticIndex:
    """Unit-length embeddings of the historical tickets, one row per ticket."""

    def __init__(self, embedder: Embedder, matrix: np.ndarray, row_ids: tuple[str, ...], source: str) -> None:
        if matrix.ndim != 2 or matrix.shape[0] != len(row_ids):
            raise SemanticError("embedding rows do not match the ticket list")
        if matrix.shape[1] != embedder.dimension:
            raise SemanticError("embedding size does not match the loaded model")
        self.embedder = embedder
        self.matrix = matrix.astype(np.float32, copy=False)
        self.row_ids = row_ids
        self.source = source  # "artifact", "artifact_partial" or "rebuilt_at_startup"
        self.computed_rows = 0  # vectors computed when this index was built

    def query(self, text: str, max_windows: int) -> tuple[np.ndarray, int]:
        """Cosine per row for one new ticket, and how many windows were needed."""
        try:
            windows = split_into_windows(text, self.embedder, max_windows)
            vectors = self.embedder.encode(windows)
        except SemanticError:
            raise
        except Exception as exc:
            raise SemanticError(f"embedding the query failed: {type(exc).__name__}") from exc
        if vectors.ndim != 2 or vectors.shape[1] != self.matrix.shape[1]:
            raise SemanticError("query embedding has an unexpected shape")
        scores = (self.matrix @ vectors.T).max(axis=1)
        return np.clip(scores.astype(np.float64), -1.0, 1.0), len(windows)

    def top_hits(self, scores: np.ndarray, top_k: int, allowed_rows: set[int] | None = None) -> list[SemanticHit]:
        rows = [row for row in range(len(self.row_ids)) if allowed_rows is None or row in allowed_rows]
        rows.sort(key=lambda row: (-scores[row], self.row_ids[row]))
        return [SemanticHit(row=row, score=float(scores[row])) for row in rows[:top_k]]


def model_signature(embedder: Embedder) -> dict:
    """Everything besides the text that decides what vector a ticket gets."""
    return {
        "model_id": embedder.model_id,
        "model_revision": embedder.revision,
        "dimension": embedder.dimension,
        "max_tokens": embedder.max_tokens,
        "normalized": True,
        "template_version": SEMANTIC_TEMPLATE_VERSION,
    }


def text_fingerprint(ticket: TicketRecord) -> str:
    """Hash of the exact text that is embedded for a past ticket."""
    return hashlib.sha256(historical_text(ticket).encode("utf-8")).hexdigest()


def expected_manifest(corpus: TicketCorpus, embedder: Embedder) -> dict:
    return {
        **model_signature(embedder),
        "rows": [{"ticket_id": t.ticket_id, "text_sha256": text_fingerprint(t)} for t in corpus.tickets],
    }


def _stored_vectors(embedder: Embedder, artifacts_dir: Path) -> dict[str, np.ndarray]:
    """Stored vectors keyed by text hash, or {} when none can be trusted.

    A stored vector is reused only when the model signature matches exactly
    and the ticket's embedded text hashes to the same value.
    """
    try:
        stored = json.loads((artifacts_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("no usable stored embeddings; recomputing")
        return {}
    try:
        signature = model_signature(embedder)
        if not isinstance(stored, dict) or any(stored.get(k) != v for k, v in signature.items()):
            logger.warning("stored embeddings were made with different model settings; recomputing")
            return {}
        rows = stored.get("rows")
        matrix = np.load(artifacts_dir / EMBEDDINGS_FILE, allow_pickle=False)
        if not isinstance(rows, list) or matrix.ndim != 2 or matrix.shape != (len(rows), embedder.dimension):
            logger.warning("stored embeddings do not match their manifest; recomputing")
            return {}
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("no usable stored embeddings; recomputing")
        return {}
    vectors: dict[str, np.ndarray] = {}
    for position, row in enumerate(rows):
        digest = row.get("text_sha256") if isinstance(row, dict) else None
        if isinstance(digest, str):
            vectors.setdefault(digest, matrix[position])
    return vectors


def build_semantic_index(
    corpus: TicketCorpus,
    embedder: Embedder,
    artifacts_dir: Path,
    extra_dirs: tuple[Path, ...] = (),
    known_vectors: dict[str, np.ndarray] | None = None,
) -> SemanticIndex:
    """Reuse every stored vector whose text and model settings are unchanged; compute the rest.

    Vectors come from the build-time artifact, any extra cache folders, and
    `known_vectors` (text hash -> vector from the index already in memory,
    made by this same embedder). A repeated or unchanged ticket costs nothing;
    a new or edited ticket is embedded on its own. The model is only used to
    encode text; it is never trained.
    """
    stored: dict[str, np.ndarray] = {}
    for directory in (artifacts_dir, *extra_dirs):
        for digest, vector in _stored_vectors(embedder, directory).items():
            stored.setdefault(digest, vector)
    for digest, vector in (known_vectors or {}).items():
        stored.setdefault(digest, vector)
    matrix = np.zeros((len(corpus.tickets), embedder.dimension), dtype=np.float32)
    missing: list[int] = []
    for row, ticket in enumerate(corpus.tickets):
        vector = stored.get(text_fingerprint(ticket))
        if vector is None:
            missing.append(row)
        else:
            matrix[row] = vector
    if missing:
        try:
            computed = embedder.encode([historical_text(corpus.tickets[row]) for row in missing])
        except Exception as exc:
            raise SemanticError(f"embedding the historical tickets failed: {type(exc).__name__}") from exc
        if computed.shape != (len(missing), embedder.dimension):
            raise SemanticError("computed embeddings have an unexpected shape")
        matrix[missing] = computed
    if not missing:
        source = "artifact"
    elif len(missing) == len(corpus.tickets):
        source = "rebuilt_at_startup"
    else:
        source = "artifact_partial"
    index = SemanticIndex(embedder, matrix, corpus.ids, source=source)
    index.computed_rows = len(missing)
    return index


def write_artifact(corpus: TicketCorpus, embedder: Embedder, artifacts_dir: Path) -> dict:
    """Store historical embeddings with their manifest (build step).

    Vectors already stored for unchanged text are kept, not recomputed.
    """
    index = build_semantic_index(corpus, embedder, artifacts_dir)
    manifest = persist_index(index, corpus, artifacts_dir)
    return {**manifest, "computed_rows": index.computed_rows, "reused_rows": len(corpus.tickets) - index.computed_rows}


def persist_index(index: SemanticIndex, corpus: TicketCorpus, directory: Path) -> dict:
    """Write an index's vectors and manifest so unchanged text is never embedded again."""
    if index.row_ids != corpus.ids:
        raise SemanticError("index rows do not match the ticket list")
    embedder = index.embedder
    manifest = expected_manifest(corpus, embedder)
    manifest["tickets_over_token_limit"] = sum(
        1 for ticket in corpus.tickets if embedder.count_tokens(historical_text(ticket)) > embedder.max_tokens
    )
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / EMBEDDINGS_FILE, index.matrix, allow_pickle=False)
    (directory / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
