"""Rerank wrapper — happy path and degraded-mode fallback."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import pytest

from tessera.adapters.errors import AdapterNetworkError
from tessera.retrieval.rerank import rerank


@dataclass
class _StubReranker:
    name: ClassVar[str] = "stub"
    model_name: str = "stub-model"
    to_raise: Exception | None = None
    scores: list[float] | None = None

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        if self.to_raise is not None:
            raise self.to_raise
        if self.scores is not None:
            return list(self.scores)
        return [1.0 - 0.1 * i for i in range(len(passages))]

    async def health_check(self) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rerank_orders_by_score_descending_with_tiebreak() -> None:
    reranker = _StubReranker(scores=[0.5, 0.9, 0.5])
    candidates = [(2, "p0"), (1, "p1"), (3, "p2")]
    outcome = await rerank(reranker, query_text="q", candidates=candidates)
    # 1 with 0.9 first; 2 and 3 tied at 0.5 — tie-break by facet_id ASC.
    assert outcome.degraded is False
    assert [r.facet_id for r in outcome.results] == [1, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rerank_empty_candidates_returns_empty() -> None:
    outcome = await rerank(_StubReranker(), query_text="q", candidates=[])
    assert outcome.results == []
    assert outcome.degraded is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_error_triggers_degraded_fallback() -> None:
    reranker = _StubReranker(to_raise=AdapterNetworkError("timeout"))
    candidates = [(1, "p0"), (2, "p1"), (3, "p2")]
    outcome = await rerank(reranker, query_text="q", candidates=candidates)
    assert outcome.degraded is True
    assert outcome.error_message is not None
    assert "AdapterNetworkError" in outcome.error_message
    # Fallback preserves input order via a monotonically decreasing score.
    assert [r.facet_id for r in outcome.results] == [1, 2, 3]
    assert outcome.results[0].score > outcome.results[1].score


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_count_mismatch_triggers_degraded_fallback() -> None:
    reranker = _StubReranker(scores=[0.5])  # only one score for two inputs
    candidates = [(1, "p0"), (2, "p1")]
    outcome = await rerank(reranker, query_text="q", candidates=candidates)
    assert outcome.degraded is True
    assert outcome.error_message is not None
    assert "1 scores for 2 candidates" in outcome.error_message
