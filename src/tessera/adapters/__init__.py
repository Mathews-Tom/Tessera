"""Model adapters: embedder and reranker protocols and reference implementations.

The package exposes the protocol shapes, registry decorators, and error types.
Concrete adapter modules (``ollama_embedder``, ``openai_embedder``,
``st_reranker``, ``cohere_reranker``) must be imported explicitly by the code
that needs them — importing ``tessera.adapters`` does not import any
individual adapter so that an all-local deployment does not drag cloud
adapter code (and their keyring surface) into the process.
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
