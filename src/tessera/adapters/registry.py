"""In-process adapter registry with decorator registration.

The registry is a narrow indirection between the retrieval pipeline and the
concrete adapter implementations. Users register an adapter class via
``@register_embedder("name")`` / ``@register_reranker("name")``; the pipeline
looks it up by name at activation time.

Registration is effectful at import time: importing
``tessera.adapters.ollama_embedder`` is what makes ``"ollama"`` resolvable via
:func:`get_embedder_class`. Importers are responsible for importing the
adapter modules they need; this module deliberately does not auto-discover so
that a configuration asking for ``"cohere"`` in an all-local deployment fails
with a clear :class:`UnknownAdapterError` rather than dragging the cohere
module (and its API-key surface) into an all-local install.
"""

from __future__ import annotations

from collections.abc import Callable

from tessera.adapters.protocol import Embedder, Reranker


class AdapterRegistryError(Exception):
    """Base class for registry failures."""


class DuplicateAdapterError(AdapterRegistryError):
    """Attempted to register two adapters under the same name."""


class UnknownAdapterError(AdapterRegistryError):
    """Requested adapter name is not registered."""


_EMBEDDERS: dict[str, type[Embedder]] = {}
_RERANKERS: dict[str, type[Reranker]] = {}


def register_embedder(name: str) -> Callable[[type[Embedder]], type[Embedder]]:
    def decorator(cls: type[Embedder]) -> type[Embedder]:
        _register_embedder(name, cls)
        return cls

    return decorator


def register_reranker(name: str) -> Callable[[type[Reranker]], type[Reranker]]:
    def decorator(cls: type[Reranker]) -> type[Reranker]:
        _register_reranker(name, cls)
        return cls

    return decorator


def get_embedder_class(name: str) -> type[Embedder]:
    try:
        return _EMBEDDERS[name]
    except KeyError as exc:
        raise UnknownAdapterError(
            f"no embedder registered under {name!r}; known: {sorted(_EMBEDDERS)}"
        ) from exc


def get_reranker_class(name: str) -> type[Reranker]:
    try:
        return _RERANKERS[name]
    except KeyError as exc:
        raise UnknownAdapterError(
            f"no reranker registered under {name!r}; known: {sorted(_RERANKERS)}"
        ) from exc


def list_embedders() -> list[str]:
    return sorted(_EMBEDDERS)


def list_rerankers() -> list[str]:
    return sorted(_RERANKERS)


def _register_embedder(name: str, cls: type[Embedder]) -> None:
    _check_name(name)
    existing = _EMBEDDERS.get(name)
    if existing is not None and existing is not cls:
        raise DuplicateAdapterError(
            f"embedder {name!r} is already registered to {existing.__qualname__}"
        )
    _EMBEDDERS[name] = cls


def _register_reranker(name: str, cls: type[Reranker]) -> None:
    _check_name(name)
    existing = _RERANKERS.get(name)
    if existing is not None and existing is not cls:
        raise DuplicateAdapterError(
            f"reranker {name!r} is already registered to {existing.__qualname__}"
        )
    _RERANKERS[name] = cls


def _check_name(name: str) -> None:
    if not name:
        raise AdapterRegistryError("adapter name must be a non-empty string")


def _reset_for_tests() -> None:
    """Clear the registry. Test-only; do not call from production code."""

    _EMBEDDERS.clear()
    _RERANKERS.clear()
