"""HTTP MCP end-to-end: auth, Origin gate, dispatch, error shape.

Drives the real :func:`serve_http_mcp` against a real vault + fake
adapters, issues a capability via the P7 API, then hits the endpoint
with ``httpx.AsyncClient`` to verify the happy path and every
off-nominal branch.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side-effect
from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon.dispatch import dispatch_tool_call
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
