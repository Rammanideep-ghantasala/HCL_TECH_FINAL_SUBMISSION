"""The relevance decision: which ranked candidates are worth showing.

Ranking answers "which past ticket is closest?". This module answers a
different question: "is it close enough to put in front of an agent?".

It works from raw evidence, not from the fused rank:

* keyword cosine    - how alike the wording is (TF-IDF)
* semantic cosine   - how alike the meaning is (embeddings)
* coverage          - how much of the new ticket's wording the past ticket
                      accounts for, with never-seen words counted against it

Each retrieval mode has its own thresholds, because the evidence available
differs. The numbers live in config/relevance.json together with a note of how
they were chosen. They are tuned on a small reviewed query set and are not
universal: a different corpus or model needs new values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

USEFUL = "useful"
RELATED = "related"
DROPPED = "dropped"

HYBRID = "hybrid"
KEYWORD_ONLY = "keyword_only"
SEMANTIC_ONLY = "semantic_only"


class RelevanceConfigError(Exception):
    """The relevance configuration file is missing or malformed."""


@dataclass(frozen=True)
class Evidence:
    keyword_cosine: float | None
    semantic_cosine: float | None
    coverage: float | None


@dataclass(frozen=True)
class RelevanceConfig:
    version: str
    # hybrid
    hybrid_useful_semantic_min: float
    hybrid_useful_coverage_min: float
    hybrid_useful_semantic_alone_min: float
    hybrid_related_semantic_min: float
    hybrid_related_keyword_min: float
    # keyword only (meaning-based search unavailable)
    keyword_useful_cosine_min: float
    keyword_useful_coverage_min: float
    keyword_related_cosine_min: float
    # semantic only (keyword index unavailable)
    semantic_useful_min: float
    semantic_related_min: float
    calibration: dict

    def decide(self, mode: str, evidence: Evidence) -> str:
        kw = evidence.keyword_cosine or 0.0
        sem = evidence.semantic_cosine if evidence.semantic_cosine is not None else -1.0
        cov = evidence.coverage or 0.0
        if mode == HYBRID:
            if sem >= self.hybrid_useful_semantic_alone_min:
                return USEFUL
            if sem >= self.hybrid_useful_semantic_min and cov >= self.hybrid_useful_coverage_min:
                return USEFUL
            if sem >= self.hybrid_related_semantic_min or kw >= self.hybrid_related_keyword_min:
                return RELATED
            return DROPPED
        if mode == KEYWORD_ONLY:
            if kw >= self.keyword_useful_cosine_min and cov >= self.keyword_useful_coverage_min:
                return USEFUL
            if kw >= self.keyword_related_cosine_min:
                return RELATED
            return DROPPED
        if mode == SEMANTIC_ONLY:
            if sem >= self.semantic_useful_min:
                return USEFUL
            if sem >= self.semantic_related_min:
                return RELATED
            return DROPPED
        raise ValueError(f"unknown retrieval mode: {mode}")


_REQUIRED = {
    "hybrid": ("useful_semantic_min", "useful_coverage_min", "useful_semantic_alone_min", "related_semantic_min", "related_keyword_min"),
    "keyword_only": ("useful_cosine_min", "useful_coverage_min", "related_cosine_min"),
    "semantic_only": ("useful_min", "related_min"),
}


def parse_relevance_config(payload: dict) -> RelevanceConfig:
    try:
        for section, keys in _REQUIRED.items():
            for key in keys:
                value = payload[section][key]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not -1.0 <= float(value) <= 1.0:
                    raise RelevanceConfigError(f"{section}.{key} must be a number between -1 and 1")
        hybrid, keyword, semantic = payload["hybrid"], payload["keyword_only"], payload["semantic_only"]
        return RelevanceConfig(
            version=str(payload["version"]),
            hybrid_useful_semantic_min=float(hybrid["useful_semantic_min"]),
            hybrid_useful_coverage_min=float(hybrid["useful_coverage_min"]),
            hybrid_useful_semantic_alone_min=float(hybrid["useful_semantic_alone_min"]),
            hybrid_related_semantic_min=float(hybrid["related_semantic_min"]),
            hybrid_related_keyword_min=float(hybrid["related_keyword_min"]),
            keyword_useful_cosine_min=float(keyword["useful_cosine_min"]),
            keyword_useful_coverage_min=float(keyword["useful_coverage_min"]),
            keyword_related_cosine_min=float(keyword["related_cosine_min"]),
            semantic_useful_min=float(semantic["useful_min"]),
            semantic_related_min=float(semantic["related_min"]),
            calibration=dict(payload.get("calibration", {})),
        )
    except (KeyError, TypeError) as exc:
        raise RelevanceConfigError(f"relevance configuration is incomplete: {exc}") from exc


def load_relevance_config(path: Path) -> RelevanceConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RelevanceConfigError(f"cannot read relevance configuration: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RelevanceConfigError("relevance configuration must be a JSON object")
    return parse_relevance_config(payload)
