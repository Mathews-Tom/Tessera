"""Model adapters: embedder and reranker protocols and reference implementations.

The package exposes the protocol shapes, registry decorators, and error types.
The concrete adapter modules (``fastembed_embedder``, ``fastembed_reranker``)
register themselves on import via ``@register_embedder`` / ``@register_reranker``;
importing ``tessera.adapters`` itself does not pull them in, so a stripped
deployment that only consumes the protocols stays small. The daemon supervisor
imports the concrete modules at startup so the registry is populated by the
time the dispatcher needs them.
"""

from tessera.adapters.errors import (
    AdapterAuthError,
    AdapterError,
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.adapters.protocol import Embedder, Reranker
from tessera.adapters.registry import (
    AdapterRegistryError,
    DuplicateAdapterError,
    UnknownAdapterError,
    get_embedder_class,
    get_reranker_class,
    list_embedders,
    list_rerankers,
    register_embedder,
    register_reranker,
)

__all__ = [
    "AdapterAuthError",
    "AdapterError",
    "AdapterModelNotFoundError",
    "AdapterNetworkError",
    "AdapterOOMError",
    "AdapterRegistryError",
    "AdapterResponseError",
    "DuplicateAdapterError",
    "Embedder",
    "Reranker",
    "UnknownAdapterError",
    "get_embedder_class",
    "get_reranker_class",
    "list_embedders",
    "list_rerankers",
    "register_embedder",
    "register_reranker",
]
