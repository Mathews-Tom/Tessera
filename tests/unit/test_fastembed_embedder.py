"""Adapter contract for ``FastEmbedEmbedder`` with the fastembed library mocked.

Real fastembed instantiation downloads ~520 MB of ONNX weights on first
use, which is not viable for unit tests. These tests substitute the
``fastembed.TextEmbedding`` constructor with a fake that yields
deterministic vectors, exercising the adapter's lazy-load, dim-check,
and error-classification paths against the documented contract.

The integration suite covers the live fastembed path separately (slow,
opt-in).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterResponseError,
)
from tessera.adapters.fastembed_embedder import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    FastEmbedEmbedder,
)


class _FakeTextEmbedding:
    """Stand-in for ``fastembed.TextEmbedding`` — yields fixed-shape vectors."""

    def __init__(self, *, dim: int = DEFAULT_DIM, **_kwargs: Any) -> None:
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> Any:
        for i, _text in enumerate(texts):
            # Deterministic vector — content does not matter; the
            # adapter only checks length.
            yield np.full(self._dim, float(i + 1), dtype=np.float32)


class _ShortVectorEmbedding:
    """Returns vectors that intentionally mismatch the configured dim."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def embed(self, texts: Sequence[str]) -> Any:
        for _text in texts:
            yield np.zeros(7, dtype=np.float32)  # not 768


def _patch_text_embedding(monkeypatch: pytest.MonkeyPatch, replacement: Any) -> None:
    monkeypatch.setattr(
        "tessera.adapters.fastembed_embedder.TextEmbedding",
        replacement,
    )


@pytest.mark.unit
def test_default_attrs_match_module_constants() -> None:
    embedder = FastEmbedEmbedder()
    assert embedder.model_name == DEFAULT_MODEL
    assert embedder.dim == DEFAULT_DIM
    assert FastEmbedEmbedder.name == "fastembed"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embed_empty_short_circuits_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty input returns an empty list and never instantiates fastembed."""

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("fastembed must not load on empty input")

    _patch_text_embedding(monkeypatch, _explode)
    embedder = FastEmbedEmbedder()
    assert await embedder.embed([]) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embed_returns_one_vector_per_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_text_embedding(monkeypatch, _FakeTextEmbedding)
    embedder = FastEmbedEmbedder()
    out = await embedder.embed(["alpha", "beta", "gamma"])
    assert len(out) == 3
    assert all(len(v) == DEFAULT_DIM for v in out)
    # Returned values are native python floats (not numpy scalars) so
    # the sqlite-vec serialiser stays out of the numpy world.
    assert all(isinstance(v[0], float) for v in out)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embed_caches_loaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call must reuse the same TextEmbedding instance."""

    instantiations = 0

    class _CountingEmbedding(_FakeTextEmbedding):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal instantiations
            instantiations += 1
            super().__init__(*args, **kwargs)

    _patch_text_embedding(monkeypatch, _CountingEmbedding)
    embedder = FastEmbedEmbedder()
    await embedder.embed(["alpha"])
    await embedder.embed(["beta", "gamma"])
    assert instantiations == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embed_dim_mismatch_raises_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_text_embedding(monkeypatch, _ShortVectorEmbedding)
    embedder = FastEmbedEmbedder(dim=DEFAULT_DIM)
    with pytest.raises(AdapterResponseError) as exc_info:
        await embedder.embed(["alpha"])
    assert "expected" in str(exc_info.value)
    assert str(DEFAULT_DIM) in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embed_unknown_model_raises_model_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fastembed raises ValueError on unsupported identifiers; the adapter
    surfaces that as AdapterModelNotFoundError so the retry policy can
    classify it correctly (terminal, not retryable)."""

    def _raises(*_a: Any, **_k: Any) -> Any:
        raise ValueError("Model not in catalog")

    _patch_text_embedding(monkeypatch, _raises)
    embedder = FastEmbedEmbedder(model_name="not-a-real-model")
    with pytest.raises(AdapterModelNotFoundError) as exc_info:
        await embedder.embed(["alpha"])
    assert "not-a-real-model" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_health_check_loads_and_runs_two_passage_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_batches: list[list[str]] = []

    class _RecordingEmbedding(_FakeTextEmbedding):
        def embed(self, texts: Sequence[str]) -> Any:
            captured_batches.append(list(texts))
            return super().embed(texts)

    _patch_text_embedding(monkeypatch, _RecordingEmbedding)
    embedder = FastEmbedEmbedder()
    await embedder.health_check()
    assert len(captured_batches) == 1
    assert len(captured_batches[0]) == 2
