"""Adapter contract for ``FastEmbedReranker`` with the fastembed library mocked.

Real ``fastembed.rerank.cross_encoder.TextCrossEncoder`` instantiation
downloads ~130 MB of ONNX weights on first use. These unit tests
substitute the constructor with a fake to exercise the adapter's
lazy-load, score-count check, error-classification, and
``is_ready`` paths against the documented contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterResponseError,
)
from tessera.adapters.fastembed_reranker import (
    DEFAULT_MODEL,
    FastEmbedReranker,
)


class _FakeCrossEncoder:
    """Stand-in for ``TextCrossEncoder`` returning len-aware fake scores."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def rerank(self, _query: str, documents: Iterable[str]) -> list[float]:
        # Score = inverse-length proxy so the test can sanity-check
        # ordering without committing to an actual model output.
        return [1.0 / (1 + len(d)) for d in documents]


def _patch_cross_encoder(monkeypatch: pytest.MonkeyPatch, replacement: Any) -> None:
    monkeypatch.setattr(
        "tessera.adapters.fastembed_reranker.TextCrossEncoder",
        replacement,
    )


@pytest.mark.unit
def test_default_attrs_match_module_constants() -> None:
    reranker = FastEmbedReranker()
    assert reranker.model_name == DEFAULT_MODEL
    assert FastEmbedReranker.name == "fastembed"


@pytest.mark.unit
def test_is_ready_false_before_load() -> None:
    reranker = FastEmbedReranker()
    assert reranker.is_ready() is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_is_ready_true_after_first_score(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cross_encoder(monkeypatch, _FakeCrossEncoder)
    reranker = FastEmbedReranker()
    await reranker.score("query", ["passage one", "passage two"])
    assert reranker.is_ready() is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_empty_short_circuits_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("fastembed must not load on empty input")

    _patch_cross_encoder(monkeypatch, _explode)
    reranker = FastEmbedReranker()
    assert await reranker.score("query", []) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_returns_one_score_per_passage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cross_encoder(monkeypatch, _FakeCrossEncoder)
    reranker = FastEmbedReranker()
    scores = await reranker.score("query", ["a", "ab", "abc"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)
    # Inverse-length: shorter passage = higher score.
    assert scores[0] > scores[1] > scores[2]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_caches_loaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    instantiations = 0

    class _CountingCrossEncoder(_FakeCrossEncoder):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal instantiations
            instantiations += 1
            super().__init__(*args, **kwargs)

    _patch_cross_encoder(monkeypatch, _CountingCrossEncoder)
    reranker = FastEmbedReranker()
    await reranker.score("q", ["a", "b"])
    await reranker.score("q", ["c", "d"])
    assert instantiations == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_count_mismatch_raises_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive check against a future fastembed regression where
    ``rerank`` returns a wrong-length score list — the pipeline relies
    on positional alignment between input passages and output scores."""

    class _WrongLength:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def rerank(self, _query: str, _documents: Iterable[str]) -> list[float]:
            return [0.5]  # always one, regardless of input

    _patch_cross_encoder(monkeypatch, _WrongLength)
    reranker = FastEmbedReranker()
    with pytest.raises(AdapterResponseError) as exc_info:
        await reranker.score("query", ["a", "b", "c"])
    assert "1 scores for 3" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_unknown_model_raises_model_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raises(*_a: Any, **_k: Any) -> Any:
        raise ValueError("Cross-encoder not in catalog")

    _patch_cross_encoder(monkeypatch, _raises)
    reranker = FastEmbedReranker(model_name="not-a-real-cross-encoder")
    with pytest.raises(AdapterModelNotFoundError) as exc_info:
        await reranker.score("q", ["a", "b"])
    assert "not-a-real-cross-encoder" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_health_check_runs_two_passage_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, list[str]]] = []

    class _RecordingCrossEncoder(_FakeCrossEncoder):
        def rerank(self, query: str, documents: Iterable[str]) -> list[float]:
            doc_list = list(documents)
            captured.append((query, doc_list))
            return [0.0] * len(doc_list)

    _patch_cross_encoder(monkeypatch, _RecordingCrossEncoder)
    reranker = FastEmbedReranker()
    await reranker.health_check()
    assert len(captured) == 1
    _query, docs = captured[0]
    assert len(docs) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_score_seed_argument_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed kwarg exists for protocol compatibility with the torch
    reranker; ONNX cross-encoder inference is deterministic on the same
    provider, so seeded vs unseeded calls must return identical scores."""

    _patch_cross_encoder(monkeypatch, _FakeCrossEncoder)
    reranker = FastEmbedReranker()
    seeded = await reranker.score("q", ["x", "yy"], seed=42)
    unseeded = await reranker.score("q", ["x", "yy"])
    assert seeded == unseeded
