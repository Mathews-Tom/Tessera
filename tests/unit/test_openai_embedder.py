"""OpenAI embedder: keyring key loading and HTTP error classification."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tessera.adapters.errors import (
    AdapterAuthError,
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.adapters.openai_embedder import (
    KEYRING_SERVICE,
    OpenAIEmbedder,
    OpenAIKeyMissingError,
)
from tessera.vault import keyring_cache


@pytest.fixture
def _stub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[tuple[str, str], str] = {(KEYRING_SERVICE, "default"): "sk-test"}

    def fake_load(service: str, username: str) -> str | None:
        return store.get((service, username))

    monkeypatch.setattr(keyring_cache, "load_password", fake_load)


def _embedder(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    dim: int = 4,
    model: str = "text-embedding-3-small",
) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        model_name=model,
        dim=dim,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [1.0, 0.0, 0.0, 0.0]},
                    {"embedding": [0.0, 1.0, 0.0, 0.0]},
                ]
            },
        )

    vectors = await _embedder(handler).embed(["a", "b"])
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_empty_input_skips_network() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    assert await _embedder(handler).embed([]) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_key_raises_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring_cache, "load_password", lambda *_a, **_k: None)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected without key")

    with pytest.raises(OpenAIKeyMissingError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyring_unavailable_raises_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(*_a: object, **_k: object) -> str:
        raise keyring_cache.KeyringUnavailableError("no backend")

    monkeypatch.setattr(keyring_cache, "load_password", fake_load)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    with pytest.raises(OpenAIKeyMissingError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_401_is_auth_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(AdapterAuthError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_429_is_oom_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(AdapterOOMError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_404_is_model_not_found() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such model"})

    with pytest.raises(AdapterModelNotFoundError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_5xx_is_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(AdapterNetworkError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_vector_count_mismatch() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]})

    with pytest.raises(AdapterResponseError, match="expected 2"):
        await _embedder(handler).embed(["a", "b"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_dim_mismatch() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    with pytest.raises(AdapterResponseError, match="dim=2"):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_missing_data_field() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    with pytest.raises(AdapterResponseError, match="missing list 'data'"):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_non_json() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(AdapterResponseError, match="non-JSON"):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_embed_transport_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(AdapterNetworkError):
        await _embedder(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_404_model_not_found() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(AdapterModelNotFoundError):
        await _embedder(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_auth_failure() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(AdapterAuthError):
        await _embedder(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/text-embedding-3-small")
        return httpx.Response(200, json={"id": "text-embedding-3-small"})

    await _embedder(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_non_200_non_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(AdapterResponseError):
        await _embedder(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_transport_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(AdapterNetworkError):
        await _embedder(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_cached_key_reused() -> None:
    call_count = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})

    embedder = _embedder(handler)
    await embedder.embed(["a"])
    await embedder.embed(["b"])
    assert call_count["n"] == 2
    assert embedder._cached_key == "sk-test"
