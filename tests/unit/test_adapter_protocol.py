"""Runtime-checkable protocol conformance for the bundled adapters.

After the v0.3 ONNX-only switch, fastembed is the sole shipped embedder
and reranker. The protocol guard here catches a regression where the
fastembed adapter drops a required method before the retrieval pipeline
tries to call it. The registry name lookup confirms the
``@register_embedder("fastembed")`` / ``@register_reranker("fastembed")``
decorators wired both classes under the expected slot.
"""

from __future__ import annotations

import pytest

from tessera.adapters.protocol import Embedder, Reranker


@pytest.mark.unit
def test_fastembed_embedder_is_embedder() -> None:
    from tessera.adapters.fastembed_embedder import FastEmbedEmbedder

    adapter = FastEmbedEmbedder(model_name="nomic-ai/nomic-embed-text-v1.5", dim=768)
    assert isinstance(adapter, Embedder)


@pytest.mark.unit
def test_fastembed_reranker_is_reranker() -> None:
    from tessera.adapters.fastembed_reranker import FastEmbedReranker

    adapter = FastEmbedReranker()
    assert isinstance(adapter, Reranker)


@pytest.mark.unit
def test_reference_adapters_registered_by_name() -> None:
    from tessera.adapters.fastembed_embedder import FastEmbedEmbedder
    from tessera.adapters.fastembed_reranker import FastEmbedReranker
    from tessera.adapters.registry import get_embedder_class, get_reranker_class

    assert get_embedder_class("fastembed") is FastEmbedEmbedder
    assert get_reranker_class("fastembed") is FastEmbedReranker
