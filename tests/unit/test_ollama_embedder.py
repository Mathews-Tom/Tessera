"""Ollama embedder: error classification and dim validation."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tessera.adapters.errors import (
    AdapterModelNotFoundError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.adapters.ollama_embedder import KEEP_ALIVE_FOREVER, OllamaEmbedder


def _embedder_with(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaEmbedder:
    return OllamaEmbedder(
        model_name="nomic-embed-text",
        dim=4,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_returns_float_vector() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3, 0.4]})

    embedder = _embedder_with(handler)
    vectors = await embedder.embed(["hello"])
    assert vectors == [[0.1, 0.2, 0.3, 0.4]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_empty_input_skips_http() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for empty input")

    embedder = _embedder_with(handler)
    assert await embedder.embed([]) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_dim_mismatch_is_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": [0.1, 0.2]})

    with pytest.raises(AdapterResponseError, match="dim=2"):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_missing_field_is_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"something_else": []})

    with pytest.raises(AdapterResponseError, match="missing float-list"):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_404_is_model_not_found() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(AdapterModelNotFoundError):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_500_oom_is_oom_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="runtime error: out of memory")

    with pytest.raises(AdapterOOMError):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_500_generic_is_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    with pytest.raises(AdapterNetworkError):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_non_200_non_error_is_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, text="see other")

    with pytest.raises(AdapterResponseError):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_non_json_body_is_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(AdapterResponseError, match="non-JSON"):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_json_array_body_is_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with pytest.raises(AdapterResponseError, match="not a JSON object"):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_transport_error_is_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AdapterNetworkError):
        await _embedder_with(handler).embed(["hi"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_ok_when_model_tagged_latest() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "nomic-embed-text:latest"}]})

    await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_ok_when_model_present_bare() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "nomic-embed-text"}]})

    await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_missing_model_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "other-model"}]})

    with pytest.raises(AdapterModelNotFoundError):
        await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_non_200_raises_response_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(AdapterResponseError):
        await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_malformed_models_list_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": "not-a-list"})

    with pytest.raises(AdapterResponseError):
        await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_transport_error_is_network_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AdapterNetworkError):
        await _embedder_with(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_embed_preserves_order() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read().decode()
        if "alpha" in body:
            return httpx.Response(200, json={"embedding": [1.0, 0.0, 0.0, 0.0]})
        if "beta" in body:
            return httpx.Response(200, json={"embedding": [0.0, 1.0, 0.0, 0.0]})
        return httpx.Response(200, json={"embedding": [0.0, 0.0, 1.0, 0.0]})

    vectors = await _embedder_with(handler).embed(["alpha", "beta", "gamma"])
    assert vectors[0] == [1.0, 0.0, 0.0, 0.0]
    assert vectors[1] == [0.0, 1.0, 0.0, 0.0]
    assert vectors[2] == [0.0, 0.0, 1.0, 0.0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_pins_model_with_keep_alive_forever() -> None:
    # Prevent regression of the warm-keep behaviour: every /api/embeddings
    # POST must carry keep_alive=-1 so a daemon that idles between recalls
    # does not pay the Ollama cold-load tax (~2-5 s for nomic-embed-text).
    import json as _json

    captured_bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_bodies.append(_json.loads(req.read().decode()))
        return httpx.Response(200, json={"embedding": [0.0, 0.0, 0.0, 0.0]})

    await _embedder_with(handler).embed(["warm"])
    assert captured_bodies, "embed call did not reach the transport"
    assert captured_bodies[0].get("keep_alive") == KEEP_ALIVE_FOREVER
