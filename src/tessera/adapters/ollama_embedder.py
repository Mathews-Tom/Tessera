"""Reference embedder: Ollama at ``http://localhost:11434``.

The adapter is the low-level boundary — one HTTP call, classified on error.
Retries and backoff per ``docs/system-design.md §Failure taxonomy`` live in
the embed worker (P3), not here: mixing retry policy into the adapter would
duplicate it across every provider and make the retry strategy untestable at
the level that actually matters (the capture-to-vec-row pipeline).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.adapters.registry import register_embedder

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Ollama unloads idle models after 5 minutes by default. A daemon that
# services one recall every few minutes would pay a cold-load penalty
# (~2-5 s for nomic-embed-text on M1 Pro) on every call - fine for
# benchmarks that run back-to-back trials, catastrophic for real-user
# latency. ``keep_alive=-1`` pins the model for the lifetime of the
# Ollama daemon; the integer wire format is documented in Ollama's API
# reference and interpreted as "never unload".
KEEP_ALIVE_FOREVER = -1


@register_embedder("ollama")
@dataclass
class OllamaEmbedder:
    """Embed via the Ollama ``/api/embeddings`` endpoint.

    ``dim`` is user-supplied: it is a property of the chosen ``model_name``
    (e.g. ``nomic-embed-text`` → 768) and is registered once when the model is
    added to the vault's ``embedding_models`` table. An observed-vs-registered
    dim mismatch is raised as :class:`~tessera.adapters.errors.AdapterResponseError`.
    """

    name: ClassVar[str] = "ollama"

    model_name: str
    dim: int
    host: str = DEFAULT_HOST
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # /api/embeddings accepts a single prompt; batch by issuing parallel
        # requests with a single shared client. Ollama serialises requests
        # internally against one loaded model, so this does not multiply GPU
        # pressure — it does cut the wire-latency floor for batches.
        async with self._client() as client:
            results = await asyncio.gather(*(self._embed_one(client, text) for text in texts))
        return list(results)

    async def health_check(self) -> None:
        async with self._client() as client:
            try:
                resp = await client.get("/api/tags")
            except httpx.HTTPError as exc:
                raise AdapterNetworkError(f"ollama unreachable at {self.host}: {exc}") from exc
        if resp.status_code != 200:
            raise AdapterResponseError(
                f"ollama /api/tags returned {resp.status_code}: {resp.text[:200]}"
            )
        payload = _parse_json(resp)
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise AdapterResponseError("ollama /api/tags response has non-list 'models' field")
        known = {m.get("name") for m in models if isinstance(m, dict)}
        # Ollama tag names are ``model:tag``; accept both "nomic-embed-text" and
        # "nomic-embed-text:latest" as matches for the registered model_name.
        if not any(n == self.model_name or n == f"{self.model_name}:latest" for n in known):
            raise AdapterModelNotFoundError(
                f"ollama has no model named {self.model_name!r}; run `ollama pull {self.model_name}`"
            )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.host,
            timeout=self.timeout_seconds,
            transport=self.transport,
        )

    async def _embed_one(self, client: httpx.AsyncClient, text: str) -> list[float]:
        try:
            resp = await client.post(
                "/api/embeddings",
                json={
                    "model": self.model_name,
                    "prompt": text,
                    "keep_alive": KEEP_ALIVE_FOREVER,
                },
            )
        except httpx.HTTPError as exc:
            raise AdapterNetworkError(f"ollama embed call failed: {exc}") from exc
        if resp.status_code == 404:
            raise AdapterModelNotFoundError(
                f"ollama has no model {self.model_name!r} (404 from /api/embeddings)"
            )
        if resp.status_code >= 500:
            body = resp.text.lower()
            if "out of memory" in body or "oom" in body:
                raise AdapterOOMError(f"ollama reported OOM: {resp.text[:200]}")
            raise AdapterNetworkError(f"ollama 5xx: {resp.status_code} {resp.text[:200]}")
        if resp.status_code != 200:
            raise AdapterResponseError(f"ollama non-200: {resp.status_code} {resp.text[:200]}")
        payload = _parse_json(resp)
        embedding = payload.get("embedding")
        if not isinstance(embedding, list) or not all(
            isinstance(v, int | float) for v in embedding
        ):
            raise AdapterResponseError("ollama response missing float-list 'embedding'")
        if len(embedding) != self.dim:
            raise AdapterResponseError(
                f"ollama returned dim={len(embedding)}, registered dim={self.dim}"
            )
        return [float(v) for v in embedding]


def _parse_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AdapterResponseError(f"ollama returned non-JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterResponseError("ollama response is not a JSON object")
    return payload
