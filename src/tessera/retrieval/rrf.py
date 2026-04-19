"""Reciprocal Rank Fusion.

RRF merges multiple ranked lists by summing ``1/(k + rank_i)`` across
each list a document appears in. It is scale-invariant (no score
calibration between BM25 and cosine distance is required) and robust to
list-length differences. The deterministic tie-break is ``facet_id``
ascending so two documents with identical RRF score always order the
same way — a prerequisite for the determinism CI job described in
``docs/determinism-and-observability.md``.

``k`` defaults to 60 per the Cormack et al. (2009) paper that introduced
the method; configurable so the P12 benchmarks can sweep the parameter
if it turns out to matter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


class _Ranked(Protocol):
    # Read-only attributes so frozen dataclasses (BM25Candidate,
    # DenseCandidate) structurally satisfy the protocol. A settable-attr
    # protocol would reject frozen instances under mypy's strict mode.
    @property
    def facet_id(self) -> int: ...
    @property
    def rank(self) -> int: ...


@dataclass(frozen=True, slots=True)
class RRFResult:
    facet_id: int
    score: float
    rank: int


DEFAULT_K: int = 60


def fuse(*lists: Iterable[_Ranked], k: int = DEFAULT_K) -> list[RRFResult]:
    """Merge ranked lists via RRF, sorted by fused score then facet_id.

    Each input list must already be sorted by that stage's intrinsic score
    (the ``rank`` field encodes ordinal position). Documents absent from a
    list simply do not contribute from that list — RRF treats missing
    documents as rank = ∞.
    """

    if k <= 0:
        raise ValueError(f"k must be positive; got {k}")
    scores: dict[int, float] = {}
    for ranked in lists:
        for item in ranked:
            scores[item.facet_id] = scores.get(item.facet_id, 0.0) + 1.0 / (k + item.rank + 1)
    # RRF score is higher-is-better, tie-break by facet_id ascending.
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [
        RRFResult(facet_id=fid, score=score, rank=idx) for idx, (fid, score) in enumerate(ordered)
    ]
