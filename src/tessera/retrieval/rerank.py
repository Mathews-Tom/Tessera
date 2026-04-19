"""Cross-encoder reranking with degraded-mode fallback.

``docs/system-design.md §Retrieval pipeline`` hard-rule 3: reranking is
mandatory. When the active reranker fails — provider outage, cold-load
timeout, per-query crash — the pipeline falls back to the RRF-order
result list and writes an audit entry tagged ``retrieval_rerank_degraded``
so the failure is visible to operators rather than silently skipped.
This module owns that fallback path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tessera.adapters.errors import AdapterError
from tessera.adapters.protocol import Reranker


@dataclass(frozen=True, slots=True)
class RerankedCandidate:
    facet_id: int
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    results: list[RerankedCandidate]
    degraded: bool
    error_message: str | None


async def rerank(
    reranker: Reranker,
    *,
    query_text: str,
    candidates: Sequence[tuple[int, str]],
    seed: int | None = None,
) -> RerankOutcome:
    """Return reranked (facet_id, score) ordered descending by score.

    ``candidates`` is a sequence of ``(facet_id, content)`` pairs in
    RRF-fused order. On adapter error the input ordering is preserved,
    scores synthesised as a strictly-decreasing 1.0, 0.99, 0.98, …
    sequence so downstream ``sort-by-score`` stages do not invert the
    fallback order.
    """

    if not candidates:
        return RerankOutcome(results=[], degraded=False, error_message=None)
    passages = [content for _, content in candidates]
    try:
        scores = await reranker.score(query_text, passages, seed=seed)
    except AdapterError as exc:
        fallback = _synthesise_fallback(candidates)
        return RerankOutcome(
            results=fallback,
            degraded=True,
            error_message=f"{type(exc).__name__}: {exc}",
        )
    if len(scores) != len(candidates):
        fallback = _synthesise_fallback(candidates)
        return RerankOutcome(
            results=fallback,
            degraded=True,
            error_message=(
                f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
            ),
        )
    # Score descending, tie-break on facet_id ascending.
    indexed = [
        (facet_id, float(score)) for (facet_id, _), score in zip(candidates, scores, strict=True)
    ]
    ordered = sorted(indexed, key=lambda pair: (-pair[1], pair[0]))
    return RerankOutcome(
        results=[
            RerankedCandidate(facet_id=fid, score=score, rank=idx)
            for idx, (fid, score) in enumerate(ordered)
        ],
        degraded=False,
        error_message=None,
    )


def _synthesise_fallback(
    candidates: Sequence[tuple[int, str]],
) -> list[RerankedCandidate]:
    # 1.0, 0.99, 0.98, … preserves the RRF ordering under any later
    # sort-by-score. The exact numeric gap is not semantically meaningful;
    # the MMR stage uses these as weights but also consults embeddings,
    # so a synthetic gradient is enough to keep ordering stable.
    out: list[RerankedCandidate] = []
    for idx, (facet_id, _content) in enumerate(candidates):
        out.append(
            RerankedCandidate(
                facet_id=facet_id,
                score=max(0.0, 1.0 - idx * 0.01),
                rank=idx,
            )
        )
    return out
