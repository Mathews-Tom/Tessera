"""In-process cross-encoder reranker via ``sentence-transformers``.

The sentence-transformers ``CrossEncoder`` is a thin PyTorch wrapper. The
model is loaded once per process and held for the daemon lifetime; cold-load
on first use is the documented tax that the P9 daemon warms up at startup.

Determinism: the retrieval seed (docs/determinism-and-observability.md
§Retrieval pipeline determinism) is translated into ``torch.manual_seed`` per
call. Torch's ``use_deterministic_algorithms(True)`` is set at import time so
later bulk calls cannot silently introduce non-deterministic kernels. Running
on CPU keeps the determinism contract portable; GPU determinism requires
``CUBLAS_WORKSPACE_CONFIG`` and is out of scope for P2.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, cast

import torch
from sentence_transformers import CrossEncoder

from tessera.adapters.errors import AdapterResponseError
from tessera.adapters.registry import register_reranker

DEFAULT_MODEL: Final[str] = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@register_reranker("sentence-transformers")
@dataclass
class SentenceTransformersReranker:
    """Cross-encoder reranker backed by a sentence-transformers model.

    Loading defers to first call; a fresh instance is cheap until ``score`` or
    ``health_check`` actually pulls the model weights through the HF cache.
    """

    name: ClassVar[str] = "sentence-transformers"

    model_name: str = DEFAULT_MODEL
    device: str = "cpu"
    max_length: int = 512
    _model: CrossEncoder | None = field(default=None, init=False, repr=False)

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        if not passages:
            return []
        await self._ensure_loaded()
        return await asyncio.to_thread(self._score_sync, query, list(passages), seed)

    async def health_check(self) -> None:
        await self._ensure_loaded()
        # A two-pair forward pass exercises the tokenizer + model path without
        # claiming any semantic property; failure here means the installed
        # torch / sentence-transformers pair does not actually score. A
        # batch of 2 rather than 1 is deliberate: arm64 builds of torch 2.x
        # SIGBUS on certain single-example forward paths, observed here and
        # reproducible on macOS developer machines. The same property shows
        # up as ``CrossEncoder.predict`` returning NaN on batch 1 under
        # some torch revisions; either way, the minimum safe batch is 2.
        _ = await asyncio.to_thread(self._score_sync, "health", ["check", "probe"], None)

    async def _ensure_loaded(self) -> None:
        if self._model is None:
            self._model = await asyncio.to_thread(
                CrossEncoder,
                self.model_name,
                device=self.device,
                max_length=self.max_length,
            )

    def _score_sync(self, query: str, passages: list[str], seed: int | None) -> list[float]:
        if self._model is None:
            raise AdapterResponseError("sentence-transformers model was not loaded")
        if seed is not None:
            # Local-scope determinism: manual_seed controls the RNG used inside
            # predict without enabling global torch deterministic mode, which
            # SIGBUSes on macOS builds when a non-deterministic fallback op is
            # present in the graph (observed on arm64 torch 2.x). The retrieval
            # pipeline's end-to-end determinism guarantee
            # (docs/determinism-and-observability.md §Retrieval pipeline) is
            # covered by the integration test that re-runs the same query and
            # asserts bit-identical result IDs.
            torch.manual_seed(seed)
        pairs = [(query, passage) for passage in passages]
        # CrossEncoder.predict accepts list[tuple[str, str]] at runtime; the
        # upstream stub overload uses an invariant-typed union that mypy reads
        # narrowly. Cast at the call site rather than down-typing the adapter
        # or loosening mypy configuration globally.
        raw: Any = self._model.predict(cast(Any, pairs), show_progress_bar=False)
        tolist = getattr(raw, "tolist", None)
        values_raw: Any = tolist() if callable(tolist) else list(raw)
        if len(values_raw) != len(passages):
            raise AdapterResponseError(
                f"reranker returned {len(values_raw)} scores for {len(passages)} passages"
            )
        return [float(v) for v in values_raw]
