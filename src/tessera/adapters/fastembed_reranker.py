"""In-process cross-encoder reranker backed by ``fastembed``.

fastembed wraps cross-encoder ONNX exports with the same load + ONNX
Runtime path the embedder uses. Default model is
``Xenova/ms-marco-MiniLM-L-12-v2`` — an L-12 cross-encoder from the
MS MARCO MiniLM family. Twelve layers vs. the older L-6 default give
a measurable relevance lift on MS-MARCO benchmarks at a small size
delta (~130 MB vs. ~80 MB on disk).

Loading is lazy: a fresh adapter is cheap, the ONNX session loads on
first ``score`` call. The daemon supervisor warms the embedder
synchronously and the reranker as a background task so the first
recall after startup does not pay both cold-load costs in series; see
``daemon/supervisor.py:_warm_adapters``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final

from fastembed.rerank.cross_encoder import TextCrossEncoder

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterResponseError,
)
from tessera.adapters.registry import register_reranker

DEFAULT_MODEL: Final[str] = "Xenova/ms-marco-MiniLM-L-12-v2"


@register_reranker("fastembed")
@dataclass
class FastEmbedReranker:
    """ONNX cross-encoder reranker for the registry's ``"fastembed"`` slot.

    Score semantics follow the cross-encoder norm: higher = more
    relevant; absolute values are model-defined and only meaningful
    inside one ``score`` call. The retrieval pipeline uses scores for
    relative ordering only, so absent calibration is fine.

    The ``seed`` argument is accepted to satisfy the protocol but
    ignored — ONNX cross-encoder inference is deterministic on the
    same provider, so the seeded-determinism path the torch reranker
    needed (``torch.manual_seed`` to control stochastic kernels) does
    not apply here.
    """

    name: ClassVar[str] = "fastembed"

    model_name: str = DEFAULT_MODEL
    cache_dir: str | None = None
    _model: TextCrossEncoder | None = field(default=None, init=False, repr=False)

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del seed
        if not passages:
            return []
        await self._ensure_loaded()
        return await asyncio.to_thread(self._score_sync, query, list(passages))

    async def health_check(self) -> None:
        """Force model load + a two-passage scoring pass.

        Two passages, not one, because ONNX cross-encoder forward
        passes have NaN'd on single-row inputs in some older
        onnxruntime builds; the project carried the same two-passage
        workaround for the torch CrossEncoder. A real recall always
        feeds at least two passages, so this is the realistic
        smallest viable batch.
        """

        await self._ensure_loaded()
        _ = await asyncio.to_thread(self._score_sync, "health", ["check", "probe"])

    def is_ready(self) -> bool:
        """True after the ONNX session has been instantiated.

        Surfaced for the supervisor's "warmup in background, recall
        degrades to RRF until ready" path. ``False`` means the next
        ``score`` call will pay the cold-load cost; the retrieval
        pipeline's degradation path treats that as
        ``rerank_degraded=True`` for the audit trail.
        """

        return self._model is not None

    async def _ensure_loaded(self) -> None:
        if self._model is None:
            try:
                self._model = await asyncio.to_thread(
                    TextCrossEncoder,
                    model_name=self.model_name,
                    cache_dir=self.cache_dir,
                )
            except ValueError as exc:
                raise AdapterModelNotFoundError(
                    f"fastembed has no cross-encoder {self.model_name!r}; "
                    f"see TextCrossEncoder.list_supported_models() for the catalog"
                ) from exc

    def _score_sync(self, query: str, passages: list[str]) -> list[float]:
        if self._model is None:
            raise AdapterResponseError("fastembed cross-encoder was not loaded")
        scores = list(self._model.rerank(query, passages))
        if len(scores) != len(passages):
            raise AdapterResponseError(
                f"reranker returned {len(scores)} scores for {len(passages)} passages"
            )
        return [float(s) for s in scores]


__all__ = ["DEFAULT_MODEL", "FastEmbedReranker"]
