"""Cloud reranker: Cohere ``/v1/rerank``.

Opt-in. Forwards (query, passages) to Cohere's rerank API. The API key is
loaded from the OS keyring only — environment variables and config files are
never consulted so that a co-located process reading ``config.yaml`` cannot
exfiltrate the key (docs/threat-model.md §S5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import httpx

from tessera.adapters.errors import (
    AdapterAuthError,
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.adapters.registry import register_reranker
from tessera.vault import keyring_cache

DEFAULT_HOST: Final[str] = "https://api.cohere.ai"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

KEYRING_SERVICE: Final[str] = "tessera-adapter-cohere"


class CohereKeyMissingError(AdapterAuthError):
    """No Cohere API key was found in the keyring."""


@register_reranker("cohere")
@dataclass
class CohereReranker:
    """Rerank via Cohere's ``/v1/rerank`` endpoint."""

    name: ClassVar[str] = "cohere"

    model_name: str = "rerank-english-v3.0"
    key_handle: str = "default"
    host: str = DEFAULT_HOST
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None
    _cached_key: str | None = field(default=None, init=False, repr=False)

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,  # Cohere is stateless; seed is ignored
    ) -> list[float]:
        if not passages:
            return []
        headers = {"Authorization": f"Bearer {self._load_key()}"}
        async with httpx.AsyncClient(
            base_url=self.host,
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self.transport,
        ) as client:
            try:
                resp = await client.post(
                    "/v1/rerank",
                    json={
                        "model": self.model_name,
                        "query": query,
                        "documents": list(passages),
                        "return_documents": False,
                        "top_n": len(passages),
                    },
                )
            except httpx.HTTPError as exc:
                raise AdapterNetworkError(f"cohere rerank call failed: {exc}") from exc
        self._check_status(resp)
        return self._parse_scores(resp, expected=len(passages))

    async def health_check(self) -> None:
        headers = {"Authorization": f"Bearer {self._load_key()}"}
        async with httpx.AsyncClient(
            base_url=self.host,
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self.transport,
        ) as client:
            try:
                resp = await client.post(
                    "/v1/rerank",
                    json={
                        "model": self.model_name,
                        "query": "health",
                        "documents": ["check"],
                        "top_n": 1,
                    },
                )
            except httpx.HTTPError as exc:
                raise AdapterNetworkError(f"cohere unreachable at {self.host}: {exc}") from exc
        self._check_status(resp)

    def _load_key(self) -> str:
        if self._cached_key is not None:
            return self._cached_key
        try:
            raw = keyring_cache.load_password(KEYRING_SERVICE, self.key_handle)
        except keyring_cache.KeyringUnavailableError as exc:
            raise CohereKeyMissingError(f"keyring unavailable: {exc}") from exc
        if raw is None:
            raise CohereKeyMissingError(f"no cohere key stored under handle {self.key_handle!r}")
        self._cached_key = raw
        return raw

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            raise AdapterAuthError(f"cohere rejected key: {resp.status_code}")
        if resp.status_code == 404:
            raise AdapterModelNotFoundError(f"cohere model {self.model_name!r} not found")
        if resp.status_code == 429:
            raise AdapterOOMError(f"cohere rate-limited: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise AdapterNetworkError(f"cohere 5xx: {resp.status_code} {resp.text[:200]}")
        raise AdapterResponseError(f"cohere non-200: {resp.status_code} {resp.text[:200]}")

    def _parse_scores(self, resp: httpx.Response, *, expected: int) -> list[float]:
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise AdapterResponseError(f"cohere returned non-JSON body: {exc}") from exc
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != expected:
            raise AdapterResponseError(
                f"cohere returned {len(results) if isinstance(results, list) else '?'} "
                f"results, expected {expected}"
            )
        # Cohere returns results sorted by relevance with an `index` pointing
        # back at the input position. Restore input order so the caller can
        # pair scores with the (query, passage) pair they came from.
        ordered: list[float] = [0.0] * expected
        seen = [False] * expected
        for entry in results:
            if not isinstance(entry, dict):
                raise AdapterResponseError("cohere result entry is not an object")
            idx = entry.get("index")
            score = entry.get("relevance_score")
            if not isinstance(idx, int) or not 0 <= idx < expected:
                raise AdapterResponseError(f"cohere index out of range: {idx!r}")
            if not isinstance(score, int | float):
                raise AdapterResponseError(f"cohere relevance_score not numeric: {score!r}")
            if seen[idx]:
                raise AdapterResponseError(f"cohere duplicate index {idx}")
            ordered[idx] = float(score)
            seen[idx] = True
        if not all(seen):
            raise AdapterResponseError("cohere result missing indices for some passages")
        return ordered
