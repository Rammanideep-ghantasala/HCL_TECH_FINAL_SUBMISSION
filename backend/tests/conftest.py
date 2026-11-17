"""Shared fixtures.

Most tests use a small deterministic fake embedder so they run in milliseconds
and do not depend on the model files. Tests marked `model` use the real model
and are skipped when it is not on disk.
"""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.engine import SearchEngine  # noqa: E402
from app.retrieval.semantic import SemanticError  # noqa: E402
from app.text import unigram_terms  # noqa: E402

REPO_DIR = BACKEND_DIR.parent
REAL_TICKETS = REPO_DIR / "starter_kit" / "data" / "tickets.json"
REAL_EXAMPLES = REPO_DIR / "starter_kit" / "data" / "new_tickets_example.json"
REAL_MODEL_DIR = BACKEND_DIR / "models" / "all-MiniLM-L6-v2"


class FakeEmbedder:
    """Hashes words into a small vector. Similar wording gives similar vectors."""

    model_id = "fake/bag-of-words"
    revision = "test"

    def __init__(self, dimension: int = 64, max_tokens: int = 256, fail_on_encode: bool = False) -> None:
        self.dimension = dimension
        self.max_tokens = max_tokens
        self.fail_on_encode = fail_on_encode
        self.encoded_texts: list[str] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        if self.fail_on_encode:
            raise RuntimeError("simulated model failure")
        self.encoded_texts.extend(texts)
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in unigram_terms(text):
                matrix[row, zlib.crc32(term.encode("utf-8")) % self.dimension] += 1.0
            norm = float(np.linalg.norm(matrix[row]))
            if norm == 0.0:
                matrix[row, 0] = 1.0
            else:
                matrix[row] /= norm
        return matrix

    def count_tokens(self, text: str) -> int:
        return len(text.split()) + 2


def make_ticket(ticket_id: str, subject: str, description: str, resolution: str = "Fixed it.", **extra) -> dict:
    record = {
        "ticket_id": ticket_id,
        "category": "general",
        "subject": subject,
        "description": description,
        "resolution_notes": resolution,
        "tags": ["sample"],
        "created_at": "2025-01-01",
        "status": "resolved",
    }
    record.update(extra)
    return record


SAMPLE_TICKETS = [
    make_ticket(
        "A-1",
        "Invoice total is wrong after plan change",
        "The invoice shows the old price even though the plan was downgraded last week.",
        "The downgrade had not reached billing. Corrected the invoice by hand.",
        category="billing",
        tags=["invoice", "downgrade"],
    ),
    make_ticket(
        "A-2",
        "Okta login loops back to the sign-in page",
        "After signing in with Okta the browser returns to the sign-in page with error 403.",
        "The Okta group mapping was missing. Added the mapping. See A-1 for a related billing note.",
        category="auth",
        tags=["okta", "login"],
    ),
    make_ticket(
        "A-3",
        "Export to spreadsheet never finishes",
        "Starting a spreadsheet export shows a progress bar that never completes for large reports.",
        "Large exports ran in the request. Moved them to a background job.",
        category="export",
        tags=["export"],
    ),
    make_ticket(
        "A-4",
        "Dashboard charts are blank for one account",
        "All charts on the dashboard render blank for a single account while other accounts look fine.",
        "A filter saved on the account returned no rows. Cleared the saved filter.",
        category="render",
        tags=["dashboard", "chart"],
    ),
    make_ticket(
        "A-5",
        "Password reset link has expired",
        "The reset link in the email says it has expired when opened the next morning.",
        "Reset links last one hour. Sent a new link and explained the limit.",
        category="auth",
        tags=["password-reset"],
    ),
]


def write_tickets(path: Path, tickets: list) -> Path:
    path.write_text(json.dumps({"tickets": tickets}), encoding="utf-8")
    return path


@pytest.fixture
def sample_tickets_path(tmp_path: Path) -> Path:
    return write_tickets(tmp_path / "tickets.json", SAMPLE_TICKETS)


def make_settings(tmp_path: Path, tickets_path: Path, **overrides) -> Settings:
    values = {
        "tickets_path": tickets_path,
        "examples_path": REAL_EXAMPLES,
        "model_dir": tmp_path / "no-model",
        "artifacts_dir": tmp_path / "artifacts",
        "relevance_config_path": BACKEND_DIR / "config" / "relevance.json",
        "frontend_dist": tmp_path / "no-frontend",
        "data_dir": tmp_path / "state",
        "ticket_store": "json",
    }
    values.update(overrides)
    return Settings(**values)


def build_engine(settings: Settings, embedder=None, broken_model: bool = False) -> SearchEngine:
    def factory(_: Settings):
        if broken_model:
            raise SemanticError("simulated load failure")
        return embedder if embedder is not None else FakeEmbedder()

    return SearchEngine.build(settings, embedder_factory=factory)


@pytest.fixture
def sample_engine(tmp_path: Path, sample_tickets_path: Path) -> SearchEngine:
    return build_engine(make_settings(tmp_path, sample_tickets_path))


@pytest.fixture
def real_engine(tmp_path: Path) -> SearchEngine:
    """The supplied 40 tickets with the fake embedder."""
    return build_engine(make_settings(tmp_path, REAL_TICKETS))


def pytest_collection_modifyitems(config, items):
    if (REAL_MODEL_DIR / "modules.json").is_file():
        return
    skip = pytest.mark.skip(reason="embedding model files are not on disk")
    for item in items:
        if "model" in item.keywords:
            item.add_marker(skip)
