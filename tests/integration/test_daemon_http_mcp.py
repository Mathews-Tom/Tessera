"""HTTP MCP end-to-end: auth, Origin gate, dispatch, error shape.

Drives the real :func:`serve_http_mcp` against a real vault + fake
adapters, issues a capability via the P7 API, then hits the endpoint
with ``httpx.AsyncClient`` to verify the happy path and every
off-nominal branch.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

import tessera.adapters.fastembed_embedder  # noqa: F401 — registration side-effect
from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon.dispatch import dispatch_tool_call
from tessera.daemon.exchange import NonceStore
from tessera.daemon.http_mcp import serve_http_mcp
from tessera.daemon.state import DaemonState
from tessera.mcp_surface import tools as mcp
from tessera.retrieval import embed_worker
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection

_DIM = 8


def _error_code(resp: httpx.Response) -> str:
    body = resp.json()
    error = body["error"]
    assert isinstance(error, dict)
    return str(error["code"])


@dataclass
class _HashEmbedder:
    name: ClassVar[str] = "fake"
    model_name: str = "hash-fake"
    dim: int = _DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([digest[i] / 255.0 for i in range(self.dim)])
        return out

    async def health_check(self) -> None:
        return None


@dataclass
class _LengthReranker:
    name: ClassVar[str] = "fake"
    model_name: str = "length"

    async def score(
        self, query: str, passages: Sequence[str], *, seed: int | None = None
    ) -> list[float]:
        del query, seed
        return [1.0 / (1 + len(p)) for p in passages]

    async def health_check(self) -> None:
        return None


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


async def _bootstrap_state(
    open_vault: VaultConnection, vault_path: Path
) -> tuple[DaemonState, int, str]:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01MCPHTTP', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=_DIM, activate=True
    )
    for i in range(3):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="style",
            content=f"voice sample {i}",
            source_tool="test",
            captured_at=1_000_000 + i,
        )
    while True:
        stats = await embed_worker.run_pass(
            open_vault.connection, embedder, active_model_id=model.id, batch_size=32
        )
        if stats.embedded == 0:
            break
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style", "project"], write=["style"]),
        now_epoch=1_000_000,
    )
    state = DaemonState(
        vault_path=vault_path,
        vault=open_vault,
        embedder=embedder,
        reranker=_LengthReranker(),
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id=open_vault.state.vault_id,
    )
    return state, agent_id, issued.raw_token


async def _serve(
    state: DaemonState,
    port: int,
    allowed_origins: frozenset[str] = frozenset({"http://localhost", "null"}),
) -> asyncio.AbstractServer:
    async def _dispatch(
        verified: tokens.VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return await dispatch_tool_call(state, verified, method, args)

    return await serve_http_mcp(
        host="127.0.0.1",
        port=port,
        allowed_origins=allowed_origins,
        conn=state.vault.connection,
        dispatch=_dispatch,
        now_epoch_fn=lambda: 1_000_100,
    )


async def _serve_with_dispatch(
    state: DaemonState,
    port: int,
    dispatch_fn: Any,
) -> asyncio.AbstractServer:
    return await serve_http_mcp(
        host="127.0.0.1",
        port=port,
        allowed_origins=frozenset({"http://localhost", "null"}),
        conn=state.vault.connection,
        dispatch=dispatch_fn,
        now_epoch_fn=lambda: 1_000_100,
    )


async def _serve_with_nonce_store(
    state: DaemonState,
    port: int,
    nonce_store: NonceStore,
    *,
    ready: asyncio.Event | None = None,
) -> asyncio.AbstractServer:
    async def _dispatch(
        verified: tokens.VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return await dispatch_tool_call(state, verified, method, args)

    return await serve_http_mcp(
        host="127.0.0.1",
        port=port,
        allowed_origins=frozenset({"http://localhost", "null"}),
        conn=state.vault.connection,
        dispatch=_dispatch,
        now_epoch_fn=lambda: 1_000_100,
        nonce_store=nonce_store,
        ready=ready,
    )


async def _raw_exchange(port: int, payload: bytes) -> bytes:
    """Send raw bytes over TCP and return the full response.

    httpx normalises off-nominal request shapes (malformed request
    line, invalid content-length, oversized headers) before they reach
    the server, so those branches require a hand-crafted connection.
    """

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        return await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


def _parse_raw_response(response: bytes) -> tuple[int, dict[str, Any]]:
    head, _, body = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    status = int(status_line.split(b" ")[1])
    return status, json.loads(body.decode("utf-8"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_stats_round_trip(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _agent_id, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "embed_health" in body["result"]
        assert "active_models" in body["result"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_missing_token(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/mcp", json={"method": "stats", "args": {}})
        assert resp.status_code == 401
        assert _error_code(resp) == "invalid_input"
        assert "bearer" in resp.json()["error"]["message"].lower()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_disallowed_origin(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port, allowed_origins=frozenset({"http://localhost"}))
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={
                    "Authorization": f"Bearer {raw_token}",
                    "Origin": "https://evil.example.com",
                },
            )
        assert resp.status_code == 403
        assert _error_code(resp) == "scope_denied"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_non_post(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 405
        assert _error_code(resp) == "invalid_input"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_unknown_route(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp/nope",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 404
        assert _error_code(resp) == "unknown_method"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_invalid_json_body(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                content=b"not json",
                headers={
                    "Authorization": f"Bearer {raw_token}",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "invalid_input"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_dispatches_recall(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={
                    "method": "recall",
                    "args": {"query_text": "voice", "k": 3},
                },
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "matches" in body["result"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_maps_validation_error(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "recall", "args": {"query_text": "voice", "k": "bad"}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "invalid_input"
        assert "k must be an integer" in resp.json()["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_maps_scope_denied(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={
                    "method": "capture",
                    "args": {"content": "denied", "facet_type": "identity"},
                },
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 403
        assert _error_code(resp) == "scope_denied"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_maps_unknown_method(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "unknown", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "unknown_method"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_maps_storage_error(open_vault: VaultConnection, vault_path: Path) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()

    async def dispatch_storage_error(
        verified: tokens.VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        del verified, method, args
        raise mcp.StorageError("storage failed")

    server = await _serve_with_dispatch(state, port, dispatch_storage_error)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 500
        assert _error_code(resp) == "storage_error"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_maps_internal_error_without_message_leak(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()

    async def dispatch_internal_error(
        verified: tokens.VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        del verified, method, args
        raise RuntimeError("secret path /tmp/private")

    server = await _serve_with_dispatch(state, port, dispatch_internal_error)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 500
        assert _error_code(resp) == "internal_error"
        assert resp.json()["error"]["message"] == "RuntimeError"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_ready_event_is_set_when_bound(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    ready = asyncio.Event()

    async def _noop_dispatch(
        verified: tokens.VerifiedCapability, method: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        del verified, method, args
        return {}

    server = await serve_http_mcp(
        host="127.0.0.1",
        port=port,
        allowed_origins=frozenset({"http://localhost"}),
        conn=state.vault.connection,
        dispatch=_noop_dispatch,
        now_epoch_fn=lambda: 1_000_100,
        ready=ready,
    )
    try:
        assert ready.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_auth_denied_token(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": "Bearer tessera_session_not-a-real-token"},
            )
        assert resp.status_code == 401
        assert _error_code(resp) == "scope_denied"
        assert resp.json()["error"]["message"] == "unauthenticated"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_non_object_body(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                content=b"[1, 2, 3]",
                headers={
                    "Authorization": f"Bearer {raw_token}",
                    "Content-Type": "application/json",
                },
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "invalid_input"
        assert "object" in resp.json()["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_wrong_method_type(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": 42, "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "invalid_input"
        assert "method must be string" in resp.json()["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_wrong_args_type(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": [1, 2]},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 400
        assert _error_code(resp) == "invalid_input"
        assert "args must be object" in resp.json()["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_returns_404_when_nonce_store_absent(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/mcp/exchange", json={"nonce": "x"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "unknown route"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_happy_path_returns_token(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    store = NonceStore()
    # Server's now_epoch_fn is pinned at 1_000_100; create the nonce
    # at 1_000_090 so expires_at (1_000_120 with 30 s TTL) is safely
    # in the future at consume time.
    entry = store.create(raw_token=raw_token, now_epoch=1_000_090)
    server = await _serve_with_nonce_store(state, port, store)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/mcp/exchange", json={"nonce": entry.nonce})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["token"] == raw_token
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_rejects_non_null_origin(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve_with_nonce_store(state, port, NonceStore())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp/exchange",
                json={"nonce": "anything"},
                headers={"Origin": "http://localhost"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"] == "origin not allowed"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_rejects_invalid_json(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve_with_nonce_store(state, port, NonceStore())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp/exchange",
                content=b"not json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid json body"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_rejects_non_object_body(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve_with_nonce_store(state, port, NonceStore())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp/exchange",
                content=b'"just a string"',
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "body must be a JSON object"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_rejects_missing_nonce(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve_with_nonce_store(state, port, NonceStore())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/mcp/exchange", json={"other": "field"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "nonce required"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_exchange_rejects_unknown_nonce(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve_with_nonce_store(state, port, NonceStore())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/mcp/exchange", json={"nonce": "bogus"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "nonce rejected"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_malformed_request_line(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        raw = await _raw_exchange(port, b"POST-only\r\nHost: x\r\n\r\n")
        status, body = _parse_raw_response(raw)
        assert status == 400
        assert body["error"]["code"] == "invalid_input"
        assert "malformed request line" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_headers_exceeding_limit(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        # 20 KiB of bogus header value trips the _MAX_HEADER_BYTES
        # gate (16 KiB) while staying under the StreamReader default
        # limit of 64 KiB; the readuntil returns a large blob and the
        # explicit len() check rejects it.
        huge_header = b"X-Filler: " + b"a" * (20 * 1024) + b"\r\n"
        payload = b"POST /mcp HTTP/1.1\r\nHost: x\r\n" + huge_header + b"\r\n"
        raw = await _raw_exchange(port, payload)
        status, body = _parse_raw_response(raw)
        assert status == 431
        assert body["error"]["code"] == "invalid_input"
        assert "headers too large" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_stream_limit_overrun(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        # The asyncio StreamReader default limit is 64 KiB; sending
        # more than that without a terminating CRLFCRLF trips
        # LimitOverrunError in readuntil, distinct from the
        # _MAX_HEADER_BYTES length check that fires post-readuntil.
        payload = b"POST /mcp HTTP/1.1\r\nX-Filler: " + b"a" * (70 * 1024)
        raw = await _raw_exchange(port, payload)
        status, body = _parse_raw_response(raw)
        assert status == 431
        assert body["error"]["code"] == "invalid_input"
        assert "headers too large" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_malformed_header_line(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        payload = b"POST /mcp HTTP/1.1\r\nHost: x\r\nNoColonHere\r\n\r\n"
        raw = await _raw_exchange(port, payload)
        status, body = _parse_raw_response(raw)
        assert status == 400
        assert body["error"]["code"] == "invalid_input"
        assert "malformed header line" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_invalid_content_length(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        payload = b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: not-a-number\r\n\r\n"
        raw = await _raw_exchange(port, payload)
        status, body = _parse_raw_response(raw)
        assert status == 400
        assert body["error"]["code"] == "invalid_input"
        assert "invalid content-length" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_rejects_oversized_body(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, _ = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        oversized = 2 * (1 << 20)  # 2 MiB, above the 1 MiB _MAX_BODY_BYTES
        payload = f"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: {oversized}\r\n\r\n".encode()
        raw = await _raw_exchange(port, payload)
        status, body = _parse_raw_response(raw)
        assert status == 413
        assert body["error"]["code"] == "invalid_input"
        assert "body too large" in body["error"]["message"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_mcp_handles_incomplete_read_silently(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, _, raw_token = await _bootstrap_state(open_vault, vault_path)
    port = _pick_port()
    server = await _serve(state, port)
    try:
        # Connect and close without sending any request bytes; server
        # must swallow the IncompleteReadError and stay healthy for
        # the next client.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        del reader
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/mcp",
                json={"method": "stats", "args": {}},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
    finally:
        server.close()
        await server.wait_closed()
