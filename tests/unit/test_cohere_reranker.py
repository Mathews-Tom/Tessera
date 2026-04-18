"""Cohere reranker: order preservation and error classification."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tessera.adapters.cohere_reranker import (
    KEYRING_SERVICE,
    CohereKeyMissingError,
    CohereReranker,
)
from tessera.adapters.errors import (
    AdapterAuthError,
    AdapterNetworkError,
    AdapterOOMError,
    AdapterResponseError,
)
from tessera.vault import keyring_cache


@pytest.fixture
def _stub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        keyring_cache,
        "load_password",
        lambda service, username: "co-test" if service == KEYRING_SERVICE else None,
    )


def _reranker(handler: Callable[[httpx.Request], httpx.Response]) -> CohereReranker:
    return CohereReranker(transport=httpx.MockTransport(handler))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_empty_passages_skips_http() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    assert await _reranker(handler).score("q", []) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_restores_input_order() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        # Cohere returns results sorted by relevance with original index.
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.7},
                    {"index": 1, "relevance_score": 0.3},
                ]
            },
        )

    scores = await _reranker(handler).score("q", ["a", "b", "c"])
    assert scores == [0.7, 0.3, 0.9]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring_cache, "load_password", lambda *_a, **_k: None)
    with pytest.raises(CohereKeyMissingError):
        await _reranker(lambda _r: httpx.Response(200)).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_keyring_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_a: object, **_k: object) -> str:
        raise keyring_cache.KeyringUnavailableError("no backend")

    monkeypatch.setattr(keyring_cache, "load_password", fake)
    with pytest.raises(CohereKeyMissingError):
        await _reranker(lambda _r: httpx.Response(200)).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_auth_failure() -> None:
    with pytest.raises(AdapterAuthError):
        await _reranker(lambda _r: httpx.Response(401)).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_rate_limit_is_oom() -> None:
    with pytest.raises(AdapterOOMError):
        await _reranker(lambda _r: httpx.Response(429)).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_5xx_is_network_error() -> None:
    with pytest.raises(AdapterNetworkError):
        await _reranker(lambda _r: httpx.Response(503)).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_result_count_mismatch() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

    with pytest.raises(AdapterResponseError, match="expected 2"):
        await _reranker(handler).score("q", ["a", "b"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_duplicate_index_rejected() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    with pytest.raises(AdapterResponseError, match="duplicate"):
        await _reranker(handler).score("q", ["a", "b"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_index_out_of_range_rejected() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 7, "relevance_score": 0.1},
                ]
            },
        )

    with pytest.raises(AdapterResponseError, match="out of range"):
        await _reranker(handler).score("q", ["a", "b"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_non_numeric_rejected() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": "high"},
                ]
            },
        )

    with pytest.raises(AdapterResponseError, match="not numeric"):
        await _reranker(handler).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_transport_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(AdapterNetworkError):
        await _reranker(handler).score("q", ["a"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_ok() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.5}]},
        )

    await _reranker(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_health_check_transport_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(AdapterNetworkError):
        await _reranker(handler).health_check()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_key")
async def test_score_non_json() -> None:
    with pytest.raises(AdapterResponseError, match="non-JSON"):
        await _reranker(lambda _r: httpx.Response(200, text="nope")).score("q", ["a"])
