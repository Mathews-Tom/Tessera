"""Embedder and reranker protocols.

These are ``typing.Protocol`` shapes: adapter implementations are duck-typed
against them rather than subclassing an ABC. The retrieval pipeline (P4) and
the model registry hold references typed as ``Embedder`` / ``Reranker`` and
never observe concrete classes.

``Extractor`` is intentionally omitted at P2. Per
``.docs/development-plan.md §P2`` the extractor slot is "stubbed in P2; full
implementation deferred to P3 only if capture needs it". Protocols without
two concrete call sites are speculative abstraction — the shape is defined
alongside its first real user in P3.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Produces dense vectors for a list of input strings.

    Implementations declare their registration name as a ``ClassVar``, expose
    the provider-side ``model_name`` and vector ``dim``, and return one vector
    of length ``dim`` per input text. Dimension drift (provider silently
    changes ``dim`` on a model update) is caught by the integration tests that
    compare returned shape against the registered ``dim``.
    """

    name: ClassVar[str]
    model_name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in input order.

        Raises :class:`~tessera.adapters.errors.AdapterNetworkError`,
        :class:`~tessera.adapters.errors.AdapterModelNotFoundError`,
        :class:`~tessera.adapters.errors.AdapterOOMError`, or
        :class:`~tessera.adapters.errors.AdapterResponseError` on failure.
        """
        ...

    async def health_check(self) -> None:
        """Verify provider reachability and model availability.

        Returns normally on success; raises an
        :class:`~tessera.adapters.errors.AdapterError` subclass otherwise.
        """
        ...


@runtime_checkable
class Reranker(Protocol):
    """Scores (query, passage) pairs for relevance.

    Score semantics are provider-defined; higher is more relevant. The
    retrieval pipeline uses these scores only for relative ordering inside a
    single call, so cross-provider score calibration is not required.

    Rerankers are called once per retrieval with the cross-encoder top-50.
    The ``seed`` argument controls torch deterministic mode for local rerankers;
    HTTP-based rerankers ignore it.
    """

    name: ClassVar[str]
    model_name: str

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        """Return one score per passage, in input order."""
        ...

    async def health_check(self) -> None:
        """Verify reranker is loaded / reachable. Raises on failure."""
        ...
