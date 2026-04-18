"""Reference embedder: OpenAI ``/v1/embeddings``.

Opt-in. Adding this adapter to a vault requires explicit user consent because
it forwards facet content to a cloud provider (see docs/threat-model.md §S5).
The API key is sourced from the OS keyring only — never from environment
variables or config files — so a co-located process reading
``config.yaml`` cannot exfiltrate it.
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
from tessera.adapters.registry import register_embedder
from tessera.vault import keyring_cache

DEFAULT_HOST: Final[str] = "https://api.openai.com"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

KEYRING_SERVICE: Final[str] = "tessera-adapter-openai"


class OpenAIKeyMissingError(AdapterAuthError):
    """No OpenAI API key was found in the keyring."""


@register_embedder("openai")
@dataclass
class OpenAIEmbedder:
    """Embed via OpenAI's ``/v1/embeddings`` endpoint.

    The key handle is a keyring username (e.g. ``"default"`` for a single-user
    install). Multiple handles allow the same vault to use different keys per
    agent without rewriting config.
    """

    name: ClassVar[str] = "openai"

    model_name: str
    dim: int
    key_handle: str = "default"
    host: str = DEFAULT_HOST
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None
    _cached_key: str | None = field(default=None, init=False, repr=False)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
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
                    "/v1/embeddings",
                    json={"model": self.model_name, "input": list(texts)},
                )
            except httpx.HTTPError as exc:
                raise AdapterNetworkError(f"openai embed call failed: {exc}") from exc
        self._check_status(resp)
        return self._parse_vectors(resp, expected=len(texts))

    async def health_check(self) -> None:
        # The list-models endpoint doubles as auth + reachability probe.
        headers = {"Authorization": f"Bearer {self._load_key()}"}
        async with httpx.AsyncClient(
            base_url=self.host,
            timeout=self.timeout_seconds,
            headers=headers,
            transport=self.transport,
        ) as client:
            try:
                resp = await client.get(f"/v1/models/{self.model_name}")
            except httpx.HTTPError as exc:
                raise AdapterNetworkError(f"openai unreachable at {self.host}: {exc}") from exc
        if resp.status_code == 404:
            raise AdapterModelNotFoundError(f"openai has no model {self.model_name!r}")
        if resp.status_code in (401, 403):
            raise AdapterAuthError(f"openai rejected key: {resp.status_code}")
        if resp.status_code != 200:
            raise AdapterResponseError(
                f"openai /v1/models returned {resp.status_code}: {resp.text[:200]}"
            )

    def _load_key(self) -> str:
        if self._cached_key is not None:
            return self._cached_key
        try:
            raw = keyring_cache.load_password(KEYRING_SERVICE, self.key_handle)
        except keyring_cache.KeyringUnavailableError as exc:
            raise OpenAIKeyMissingError(f"keyring unavailable: {exc}") from exc
        if raw is None:
            raise OpenAIKeyMissingError(
                f"no openai key stored under handle {self.key_handle!r}; "
                "store via `tessera models set embedder openai --api-key-handle ...`"
            )
        self._cached_key = raw
        return raw

    def invalidate_cached_key(self) -> None:
        """Drop the in-memory key cache so the next call re-reads the keyring.

        A long-running daemon holds an adapter instance across operations.
        Without invalidation, rotating the keyring entry (e.g. revoking a
        compromised API key) would not take effect until daemon restart.
        The CLI and daemon control plane call this after a key rotation.
        """

        self._cached_key = None

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            raise AdapterAuthError(f"openai rejected key: {resp.status_code}")
        if resp.status_code == 404:
            raise AdapterModelNotFoundError(f"openai model {self.model_name!r} not found")
        if resp.status_code == 429:
            raise AdapterOOMError(f"openai rate-limit / quota exhausted: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise AdapterNetworkError(f"openai 5xx: {resp.status_code} {resp.text[:200]}")
        raise AdapterResponseError(f"openai non-200: {resp.status_code} {resp.text[:200]}")

    def _parse_vectors(self, resp: httpx.Response, *, expected: int) -> list[list[float]]:
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise AdapterResponseError(f"openai returned non-JSON body: {exc}") from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise AdapterResponseError("openai response missing list 'data' field")
        if len(data) != expected:
            raise AdapterResponseError(f"openai returned {len(data)} vectors, expected {expected}")
        out: list[list[float]] = []
        for entry in data:
            if not isinstance(entry, dict):
                raise AdapterResponseError("openai response 'data' entry is not an object")
            emb = entry.get("embedding")
            if not isinstance(emb, list) or not all(isinstance(v, int | float) for v in emb):
                raise AdapterResponseError("openai response missing float-list 'embedding'")
            if len(emb) != self.dim:
                raise AdapterResponseError(
                    f"openai returned dim={len(emb)}, registered dim={self.dim}"
                )
            out.append([float(v) for v in emb])
        return out
