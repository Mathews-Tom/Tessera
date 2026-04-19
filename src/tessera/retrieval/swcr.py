"""Sequential Weighted Context Recall — coherence reweighting.

Implements the closed-form algorithm from ``docs/swcr-spec.md §Algorithm``.
SWCR is a post-rerank stage: it consumes per-candidate rerank scores
``s_r(f)`` and augments each one with a coherence bonus proportional to
how strongly ``f`` connects to other high-scoring candidates across the
coherence graph. Cross-type edges contribute more than same-type edges
so the boost favours facets that reinforce a multi-facet agent identity
(voice sample that matches recent episodics, skill that matches active
goals, etc).

This module is pure: no DB, no adapters, no async. The inputs are the
candidate set and the derived scores; the output is a new ranked list.
P5 ablation exists exactly because every decision here (alpha, beta,
gamma, lambda, sparsification threshold) is load-bearing for the
product's "coherence differentiator" claim — they get tuned against
``B-RET-1`` and justified in writing or the claim is retracted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class SWCRParams:
    alpha: float = 0.5  # semantic-similarity edge weight
    beta: float = 0.3  # entity-Jaccard edge weight
    gamma: float = 0.2  # cross-type bonus
    lam: float = 0.25  # coherence reweighting strength (λ in the spec)
    edge_threshold: float = 0.1  # drop edges with weight < τ_e
    max_candidates: int = 50  # M in the spec
    jaccard_epsilon: float = 1.0  # keeps Jaccard finite for empty entity sets

    def __post_init__(self) -> None:
        if not 0.0 <= self.alpha <= 1.0:
            raise ValueError(f"alpha must be in [0, 1]; got {self.alpha}")
        if not 0.0 <= self.beta <= 1.0:
            raise ValueError(f"beta must be in [0, 1]; got {self.beta}")
        if not 0.0 <= self.gamma <= 0.5:
            raise ValueError(f"gamma must be in [0, 0.5]; got {self.gamma}")
        if not 0.0 <= self.lam <= 1.0:
            raise ValueError(f"lam must be in [0, 1]; got {self.lam}")
        if not 0.0 <= self.edge_threshold <= 0.3:
            raise ValueError(f"edge_threshold must be in [0, 0.3]; got {self.edge_threshold}")
        if not 20 <= self.max_candidates <= 200:
            raise ValueError(f"max_candidates must be in [20, 200]; got {self.max_candidates}")


DEFAULT_PARAMS: Final[SWCRParams] = SWCRParams()


@dataclass(frozen=True, slots=True)
class SWCRCandidate:
    facet_id: int
    rerank_score: float
    embedding: Sequence[float]
    facet_type: str
    entities: frozenset[str]


@dataclass(frozen=True, slots=True)
class SWCRResult:
    facet_id: int
    score: float
    rank: int


def apply(
    candidates: Sequence[SWCRCandidate],
    *,
    params: SWCRParams = DEFAULT_PARAMS,
) -> list[SWCRResult]:
    """Reweight ``candidates`` by adding the cross-facet coherence bonus.

    Input order is preserved only as far as the graph construction; the
    returned list is sorted by SWCR score descending with a deterministic
    ``facet_id`` tie-break. Callers that need the original positions keep
    them on the source ``SWCRCandidate`` structs.
    """

    if not candidates:
        return []
    # Top-M cap per the spec; beyond M the graph's O(M^2) cost grows.
    top = list(candidates)[: params.max_candidates]
    n = len(top)
    weights = _coherence_graph(top, params=params)
    bonuses = [0.0] * n
    for i in range(n):
        total = 0.0
        for j in range(n):
            if i == j:
                continue
            w = weights[i][j]
            if w == 0.0:
                continue
            total += w * top[j].rerank_score
        bonuses[i] = params.lam * total
    # Tie-break: score DESC, then facet_id ASC (non-negotiable per spec).
    rescored = [(cand.facet_id, cand.rerank_score + bonuses[idx]) for idx, cand in enumerate(top)]
    rescored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [
        SWCRResult(facet_id=facet_id, score=score, rank=new_rank)
        for new_rank, (facet_id, score) in enumerate(rescored)
    ]


def _coherence_graph(
    candidates: Sequence[SWCRCandidate],
    *,
    params: SWCRParams,
) -> list[list[float]]:
    """Build the symmetric weight matrix described in spec §Coherence graph."""

    n = len(candidates)
    weights = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            w = _edge_weight(candidates[i], candidates[j], params=params)
            if w < params.edge_threshold:
                continue
            weights[i][j] = w
            weights[j][i] = w
    return weights


def _edge_weight(
    a: SWCRCandidate,
    b: SWCRCandidate,
    *,
    params: SWCRParams,
) -> float:
    semantic = _cosine(a.embedding, b.embedding)
    entity = _jaccard(a.entities, b.entities, epsilon=params.jaccard_epsilon)
    cross_type = 1.0 if a.facet_type != b.facet_type else 0.0
    return params.alpha * semantic + params.beta * entity + params.gamma * cross_type


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"embedding dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom == 0.0:
        return 0.0
    return dot / denom


def _jaccard(a: frozenset[str], b: frozenset[str], *, epsilon: float) -> float:
    intersection = len(a & b)
    union = len(a | b)
    # Spec adds ε to the denominator so two empty entity sets contribute
    # 0 rather than raising a divide-by-zero in the closed-form matrix op.
    return intersection / (union + epsilon)
