"""Integration tests for the sentence-transformers cross-encoder reranker.

Loads the real model from the HuggingFace cache; marked ``slow`` because the
first invocation pulls weights (~90 MB) and a forward pass is seconds, not
milliseconds, on CPU.
"""

from __future__ import annotations

import pytest

from tessera.adapters.st_reranker import SentenceTransformersReranker


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_st_reranker_scores_deterministically() -> None:
    reranker = SentenceTransformersReranker()
    query = "how does Tessera store agent identity?"
    passages = [
        "Tessera stores agent identity in a single-file encrypted SQLite vault.",
        "The capital of France is Paris.",
        "SQLite is a library that implements a small SQL database engine.",
    ]
    scores_a = await reranker.score(query, passages, seed=42)
    scores_b = await reranker.score(query, passages, seed=42)
    assert scores_a == scores_b
    # Passage 0 is the topically relevant one; it should score above passage 1.
    assert scores_a[0] > scores_a[1]


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_st_reranker_empty_passages_returns_empty() -> None:
    reranker = SentenceTransformersReranker()
    assert await reranker.score("q", []) == []


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_st_reranker_health_check_passes() -> None:
    reranker = SentenceTransformersReranker()
    await reranker.health_check()
