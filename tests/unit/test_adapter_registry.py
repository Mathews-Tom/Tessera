"""Tests for the Python-side adapter registry."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import ClassVar

import pytest

from tessera.adapters import registry


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    # Swap the module-level tables for the duration of the test so production
    # imports remain unaffected by tests that intentionally register duplicate
    # names or empty the tables.
    original_emb = dict(registry._EMBEDDERS)
    original_rer = dict(registry._RERANKERS)
    registry._EMBEDDERS.clear()
    registry._RERANKERS.clear()
    try:
        yield
    finally:
        registry._EMBEDDERS.clear()
        registry._RERANKERS.clear()
        registry._EMBEDDERS.update(original_emb)
        registry._RERANKERS.update(original_rer)


class _StubEmbedder:
    name: ClassVar[str] = "stub-emb"
    model_name: str = "stub"
    dim: int = 4

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    async def health_check(self) -> None:
        return None


class _OtherStubEmbedder:
    name: ClassVar[str] = "other"
    model_name: str = "other"
    dim: int = 4

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    async def health_check(self) -> None:
        return None


class _StubReranker:
    name: ClassVar[str] = "stub-rer"
    model_name: str = "stub"

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        return [0.0] * len(passages)

    async def health_check(self) -> None:
        return None


@pytest.mark.unit
def test_register_embedder_and_lookup(isolated_registry: None) -> None:
    registry.register_embedder("stub")(_StubEmbedder)
    assert registry.get_embedder_class("stub") is _StubEmbedder
    assert "stub" in registry.list_embedders()


@pytest.mark.unit
def test_register_reranker_and_lookup(isolated_registry: None) -> None:
    registry.register_reranker("stub")(_StubReranker)
    assert registry.get_reranker_class("stub") is _StubReranker
    assert "stub" in registry.list_rerankers()


@pytest.mark.unit
def test_duplicate_embedder_rejected(isolated_registry: None) -> None:
    registry.register_embedder("stub")(_StubEmbedder)
    with pytest.raises(registry.DuplicateAdapterError):
        registry.register_embedder("stub")(_OtherStubEmbedder)


@pytest.mark.unit
def test_idempotent_registration_of_same_class(isolated_registry: None) -> None:
    registry.register_embedder("stub")(_StubEmbedder)
    # Re-registering the same class under the same name is a no-op — this
    # matters for module reloads during test collection, not production.
    registry.register_embedder("stub")(_StubEmbedder)
    assert registry.get_embedder_class("stub") is _StubEmbedder


@pytest.mark.unit
def test_unknown_embedder_raises(isolated_registry: None) -> None:
    with pytest.raises(registry.UnknownAdapterError):
        registry.get_embedder_class("missing")


@pytest.mark.unit
def test_unknown_reranker_raises(isolated_registry: None) -> None:
    with pytest.raises(registry.UnknownAdapterError):
        registry.get_reranker_class("missing")


@pytest.mark.unit
def test_empty_name_rejected(isolated_registry: None) -> None:
    with pytest.raises(registry.AdapterRegistryError):
        registry.register_embedder("")(_StubEmbedder)
    with pytest.raises(registry.AdapterRegistryError):
        registry.register_reranker("")(_StubReranker)


@pytest.mark.unit
def test_list_embedders_sorted(isolated_registry: None) -> None:
    registry.register_embedder("zulu")(_StubEmbedder)
    registry.register_embedder("alpha")(_OtherStubEmbedder)
    assert registry.list_embedders() == ["alpha", "zulu"]


@pytest.mark.unit
def test_reset_for_tests_clears(isolated_registry: None) -> None:
    registry.register_embedder("stub")(_StubEmbedder)
    registry._reset_for_tests()
    assert registry.list_embedders() == []
    assert registry.list_rerankers() == []
