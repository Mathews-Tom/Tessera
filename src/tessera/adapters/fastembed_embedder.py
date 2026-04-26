"""In-process embedder backed by ``fastembed`` (ONNX Runtime).

fastembed loads sentence-transformer-style models exported to ONNX and
runs them through onnxruntime. No torch dependency, no separate model
server, no HTTP boundary — embeds happen inside ``tesserad`` on
whichever ONNX provider fastembed picks (CPU by default; CoreML on
Apple Silicon when present; CUDA when wired).

The default model is ``nomic-ai/nomic-embed-text-v1.5`` (768 dim) —
the same embedding family Tessera previously served through Ollama,
just delivered in-process instead of over HTTP. Re-registering with a
different model name is supported (any entry returned by
``TextEmbedding.list_supported_models()``); the registered ``dim``
must match the model's declared dimensionality or the integration
tests catch the drift at first embed.

Loading defers to first ``embed`` / ``health_check`` call to keep
``tesserad`` startup snappy. fastembed's ``TextEmbedding.embed`` is
synchronous and CPU-bound; we wrap it with ``asyncio.to_thread`` so a
warm-running daemon does not block its event loop on a 500-doc batch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Final

from fastembed import TextEmbedding

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterResponseError,
)
from tessera.adapters.registry import register_embedder

DEFAULT_MODEL: Final[str] = "nomic-ai/nomic-embed-text-v1.5"
DEFAULT_DIM: Final[int] = 768


@register_embedder("fastembed")
@dataclass
class FastEmbedEmbedder:
    """ONNX embedder for the registry's ``"fastembed"`` adapter slot.

    A fresh instance is cheap; the underlying ONNX session loads on
    first ``embed`` call. Once loaded, embeds run on the same session
    for the daemon's lifetime — fastembed maintains the session
    internally, so we hold one ``TextEmbedding`` instance per adapter.
    """

    name: ClassVar[str] = "fastembed"

    model_name: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    cache_dir: str | None = None
    _model: TextEmbedding | None = field(default=None, init=False, repr=False)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        await self._ensure_loaded()
        vectors = await asyncio.to_thread(self._embed_sync, list(texts))
        if any(len(vec) != self.dim for vec in vectors):
            mismatched = next(len(v) for v in vectors if len(v) != self.dim)
            raise AdapterResponseError(
                f"fastembed returned dim={mismatched}, expected {self.dim} "
                f"(model {self.model_name!r})"
            )
        return vectors

    async def health_check(self) -> None:
        """Force a model load + a single embed to surface init failures.

        fastembed downloads weights on first instantiation when missing
        from ``cache_dir``. The download is bounded (model size lives
        in ``TextEmbedding.list_supported_models()``) but can take
        several seconds the first time. Doctor calls this once at
        startup so the failure mode is "first health check warns,
        worker fills the queue once weights arrive" rather than
        "every recall surfaces the same network error."
        """

        await self._ensure_loaded()
        # Two passages because numpy + ONNX session warmup occasionally
        # NaNs on a single-passage forward pass — same workaround the
        # cross-encoder reranker carries upstream.
        _ = await asyncio.to_thread(self._embed_sync, ["health", "check"])

    async def _ensure_loaded(self) -> None:
        if self._model is None:
            try:
                self._model = await asyncio.to_thread(
                    TextEmbedding,
                    model_name=self.model_name,
                    cache_dir=self.cache_dir,
                )
            except ValueError as exc:
                # fastembed raises ValueError for unsupported model
                # identifiers — surface as model-not-found so the
                # retry policy classifies it correctly.
                raise AdapterModelNotFoundError(
                    f"fastembed has no model {self.model_name!r}; "
                    f"see TextEmbedding.list_supported_models() for the catalog"
                ) from exc

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            raise AdapterResponseError("fastembed model was not loaded")
        # fastembed yields numpy arrays; tolist() flattens to native
        # Python floats so sqlite-vec's BLOB serialiser does not have
        # to learn about numpy. Iteration order matches input order
        # per fastembed's contract.
        return [vec.tolist() for vec in self._model.embed(texts)]


__all__ = ["DEFAULT_DIM", "DEFAULT_MODEL", "FastEmbedEmbedder"]
