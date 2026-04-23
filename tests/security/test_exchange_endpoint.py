"""ChatGPT Dev Mode /mcp/exchange endpoint security tests.

Pin the claims the threat model makes about the bootstrap-URL
transport: nonce is one-time-use, 30-second TTL, Origin allowlist is
narrower than /mcp's, error shape is indistinguishable across failure
modes.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable

import httpx
import pytest

from tessera.daemon.exchange import NonceStore
from tessera.daemon.http_mcp import serve_http_mcp


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _FakeConn:
    """Empty sqlite stub — /mcp/exchange does not touch the vault."""

    def execute(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        raise NotImplementedError("exchange endpoint must not hit the vault")


async def _failing_dispatch(*_args: object, **_kwargs: object) -> dict[str, object]:
    raise AssertionError("dispatch must not be called on /mcp/exchange")


async def _serve(
    nonce_store: NonceStore | None, *, now_fn: Callable[[], int]
) -> tuple[asyncio.AbstractServer, int]:
    port = _pick_port()
    server = await serve_http_mcp(
        host="127.0.0.1",
        port=port,
        allowed_origins=frozenset({"http://localhost", "http://127.0.0.1", "null"}),
        conn=_FakeConn(),
        dispatch=_failing_dispatch,
        now_epoch_fn=now_fn,
        nonce_store=nonce_store,
    )
    return server, port


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_happy_path_returns_token() -> None:
    store = NonceStore()
    now = 1_000
    entry = store.create(raw_token="tessera_session_TOKEN", now_epoch=now)
    server, port = await _serve(store, now_fn=lambda: now)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                timeout=5.0,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["token"] == "tessera_session_TOKEN"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_is_one_time_use() -> None:
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=0)
    server, port = await _serve(store, now_fn=lambda: 0)
    try:
        async with httpx.AsyncClient() as client:
            first = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                timeout=5.0,
            )
            second = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                timeout=5.0,
            )
        assert first.status_code == 200
        assert second.status_code == 401
        assert second.json() == {"error": "nonce rejected"}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_rejects_expired_nonce() -> None:
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=0)
    # Serve with a clock past the 30-second TTL.
    server, port = await _serve(store, now_fn=lambda: entry.expires_at + 1)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                timeout=5.0,
            )
        assert resp.status_code == 401
        assert resp.json() == {"error": "nonce rejected"}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_rejects_browser_origin() -> None:
    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=0)
    server, port = await _serve(store, now_fn=lambda: 0)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                headers={"Origin": "https://evil.example.com"},
                timeout=5.0,
            )
        assert resp.status_code == 403
        # Nonce must NOT have been consumed.
        assert store.pending_count() == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_returns_404_when_store_absent() -> None:
    server, port = await _serve(nonce_store=None, now_fn=lambda: 0)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": "x"},
                timeout=5.0,
            )
        assert resp.status_code == 404
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_rejects_missing_nonce() -> None:
    store = NonceStore()
    server, port = await _serve(store, now_fn=lambda: 0)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={},
                timeout=5.0,
            )
        assert resp.status_code == 400
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.security
@pytest.mark.asyncio
async def test_exchange_error_shapes_are_indistinguishable() -> None:
    """Unknown-nonce, expired-nonce, and already-consumed-nonce return
    the same error so the caller cannot probe the store state."""

    store = NonceStore()
    entry = store.create(raw_token="tok", now_epoch=0)
    server, port = await _serve(store, now_fn=lambda: entry.expires_at + 1)
    try:
        async with httpx.AsyncClient() as client:
            unknown = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": "deadbeef" * 6},
                timeout=5.0,
            )
            expired = await client.post(
                f"http://127.0.0.1:{port}/mcp/exchange",
                json={"nonce": entry.nonce},
                timeout=5.0,
            )
        assert unknown.status_code == expired.status_code == 401
        assert unknown.json() == expired.json() == {"error": "nonce rejected"}
    finally:
        server.close()
        await server.wait_closed()
