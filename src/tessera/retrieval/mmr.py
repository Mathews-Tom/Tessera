"""Maximal Marginal Relevance diversification.

MMR iteratively selects from a candidate pool the item that maximises
``lambda*relevance - (1-lambda)*max(similarity_to_selected)``. The
result is a ranked list that spreads topical coverage rather than
repeating near-duplicates of the top relevance hit.

Tessera uses MMR after rerank to keep style samples, recent events, and
skills from crowding each other out of the same token budget. Default
``λ = 0.7`` per ``docs/system-design.md §Retrieval pipeline``. Cosine
similarity on the dense embedding is the diversity metric.

Deterministic tie-break: ``facet_id`` ascending, so two candidates with
identical MMR score pick the lower id first. This is the downstream end
of the determinism chain anchored in ``retrieval.seed``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MMRItem:
    facet_id: int
    relevance: float
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class MMRResult:
    facet_id: int
    mmr_score: float
    rank: int


DEFAULT_LAMBDA: float = 0.7


def diversify(
    items: list[MMRItem],
    *,
    k: int,
    mmr_lambda: float = DEFAULT_LAMBDA,
) -> list[MMRResult]:
    """Return up to ``k`` items in greedy MMR order."""

    if k < 0:
        raise ValueError(f"k must be non-negative; got {k}")
    if not 0.0 <= mmr_lambda <= 1.0:
        raise ValueError(f"mmr_lambda must be in [0, 1]; got {mmr_lambda}")
    if not items or k == 0:
        return []

    remaining = list(items)
    selected: list[MMRItem] = []
    out: list[MMRResult] = []
    while remaining and len(selected) < k:
        best: tuple[float, int, MMRItem] | None = None
        for candidate in remaining:
            diversity_penalty = _max_similarity(candidate, selected)
            score = mmr_lambda * candidate.relevance - (1.0 - mmr_lambda) * diversity_penalty
            # Maximise score, tie-break on facet_id ASC (encoded as
            # ``-facet_id`` because we're tracking the max of a 3-tuple).
            tie_break = -candidate.facet_id
            if best is None or (score, tie_break) > (best[0], -best[2].facet_id):
                best = (score, tie_break, candidate)
        if best is None:
            break
        picked_score, _tb, picked_item = best
        selected.append(picked_item)
        remaining.remove(picked_item)
        out.append(
            MMRResult(
                facet_id=picked_item.facet_id,
                mmr_score=picked_score,
                rank=len(out),
            )
        )
    return out


def _max_similarity(candidate: MMRItem, selected: list[MMRItem]) -> float:
    if not selected:
        return 0.0
    return max(_cosine(candidate.embedding, item.embedding) for item in selected)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    a_sq = 0.0
    b_sq = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        a_sq += x * x
        b_sq += y * y
    denom = math.sqrt(a_sq) * math.sqrt(b_sq)
    if denom == 0.0:
        return 0.0
    return dot / denom
