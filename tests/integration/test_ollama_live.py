"""Integration tests against a live local Ollama daemon.

These tests skip automatically when Ollama is not reachable, which is the
expected state in CI runners that do not ship with Ollama installed. On the
developer machine (``docs/release-spec.md §v0.1 DoD``) the tests exercise the
all-local path end-to-end: registry lookup → live embed call → dim check.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tessera.adapters.errors import AdapterModelNotFoundError
from tessera.adapters.ollama_embedder import DEFAULT_HOST, OllamaEmbedder

_EMBED_MODEL = "nomic-embed-text"


def _ollama_reachable(host: str = "127.0.0.1", port: int = 11434) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


def _ollama_has_model(model_name: str) -> bool:
    async def _probe() -> bool:
        embedder = OllamaEmbedder(model_name=model_name, dim=1, host=DEFAULT_HOST)
        try:
            await embedder.health_check()
        except AdapterModelNotFoundError:
            return False
        return True

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _ollama_reachable(),
        reason="ollama not reachable on localhost:11434",
    ),
    pytest.mark.skipif(
        _ollama_reachable() and not _ollama_has_model(_EMBED_MODEL),
        reason=f"ollama model {_EMBED_MODEL!r} not pulled",
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_embed_live() -> None:
    embedder = OllamaEmbedder(model_name="nomic-embed-text", dim=768, host=DEFAULT_HOST)
    vectors = await embedder.embed(["Tessera stores agent identity."])
    assert len(vectors) == 1
    assert len(vectors[0]) == 768
    assert all(isinstance(v, float) for v in vectors[0])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_health_check_live() -> None:
    embedder = OllamaEmbedder(model_name="nomic-embed-text", dim=768, host=DEFAULT_HOST)
    await embedder.health_check()
