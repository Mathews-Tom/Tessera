"""Runtime-checkable protocol conformance for the reference adapters.

The adapter registry stores classes; the protocol guard here catches a
registration regression where a new adapter drops a required method before
the downstream retrieval pipeline (P4) tries to call it.
"""

from __future__ import annotations

import pytest

from tessera.adapters.cohere_reranker import CohereReranker
from tessera.adapters.ollama_embedder import OllamaEmbedder
from tessera.adapters.openai_embedder import OpenAIEmbedder
from tessera.adapters.protocol import Embedder, Reranker
from tessera.adapters.st_reranker import SentenceTransformersReranker


@pytest.mark.unit
def test_ollama_embedder_is_embedder() -> None:
    adapter = OllamaEmbedder(model_name="nomic-embed-text", dim=768)
    assert isinstance(adapter, Embedder)


@pytest.mark.unit
def test_openai_embedder_is_embedder() -> None:
    adapter = OpenAIEmbedder(model_name="text-embedding-3-small", dim=1536)
    assert isinstance(adapter, Embedder)


@pytest.mark.unit
def test_st_reranker_is_reranker() -> None:
    adapter = SentenceTransformersReranker()
    assert isinstance(adapter, Reranker)


@pytest.mark.unit
def test_cohere_reranker_is_reranker() -> None:
    adapter = CohereReranker()
    assert isinstance(adapter, Reranker)


@pytest.mark.unit
def test_reference_adapters_registered_by_name() -> None:
    from tessera.adapters.registry import get_embedder_class, get_reranker_class

    # Importing the adapter modules above registers their names.
    assert get_embedder_class("ollama") is OllamaEmbedder
    assert get_embedder_class("openai") is OpenAIEmbedder
    assert get_reranker_class("sentence-transformers") is SentenceTransformersReranker
    assert get_reranker_class("cohere") is CohereReranker
