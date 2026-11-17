"""Reciprocal Rank Fusion (Cormack, Clarke and Buettcher, SIGIR 2009).

fused(ticket) = sum over rankings of 1 / (k + rank), with rank starting at 1.

Only ranks are used, never raw scores. The fused value therefore says nothing
about whether a ticket is relevant: the best of a bad list gets the same value
as the best of a good list. It is used for ordering only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FusedCandidate:
    ticket_id: str
    rrf: float
    keyword_rank: int | None
    semantic_rank: int | None


def reciprocal_rank_fusion(
    keyword_ranking: list[str],
    semantic_ranking: list[str],
    k: int = 60,
) -> list[FusedCandidate]:
    """Merge two ranked lists of ticket ids into one deterministic ordering.

    Each ticket appears once. Ties are broken by the better single rank, then
    by ticket id, so the same inputs always give the same order.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    keyword_rank = _first_ranks(keyword_ranking)
    semantic_rank = _first_ranks(semantic_ranking)
    fused: list[FusedCandidate] = []
    for ticket_id in sorted(set(keyword_rank) | set(semantic_rank)):
        kw = keyword_rank.get(ticket_id)
        sem = semantic_rank.get(ticket_id)
        score = (1.0 / (k + kw) if kw else 0.0) + (1.0 / (k + sem) if sem else 0.0)
        fused.append(FusedCandidate(ticket_id=ticket_id, rrf=score, keyword_rank=kw, semantic_rank=sem))
    fused.sort(key=_order_key)
    return fused


def _first_ranks(ranking: list[str]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for position, ticket_id in enumerate(ranking, start=1):
        ranks.setdefault(ticket_id, position)
    return ranks


def _order_key(candidate: FusedCandidate) -> tuple:
    ranks = [rank for rank in (candidate.keyword_rank, candidate.semantic_rank) if rank]
    return (-round(candidate.rrf, 12), min(ranks), candidate.ticket_id)
