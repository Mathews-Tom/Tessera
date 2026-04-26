"""REST surface end-to-end: ``/api/v1/*`` happy paths + error envelope shape.

Same daemon, same dispatcher, same auth as :mod:`test_daemon_http_mcp` —
exercised through the REST router instead of the JSON-RPC ``/mcp``
endpoint. The response shape is intentionally lean (result dict directly
on success, ``{"error": {"code", "message"}}`` on failure) — the assertions
below are the wire contract REST clients can rely on.
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

import tessera.adapters.fastembed_embedder  # noqa: F401 — registration side-effect
from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon.dispatch import dispatch_tool_call
from tessera.daemon.http_mcp import serve_http_mcp
from tessera.daemon.state import DaemonState
from tessera.retrieval import embed_worker
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection

_DIM = 8


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
) -> tuple[DaemonState, int, str, list[str]]:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01RESTAPI', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=_DIM, activate=True
    )
    external_ids: list[str] = []
    for i in range(3):
        result = vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="style",
            content=f"rest sample {i}",
            source_tool="test",
            captured_at=1_000_000 + i,
        )
        external_ids.append(result.external_id)
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
        scope=build_scope(
            read=["style", "project", "preference", "workflow", "identity"],
            write=["style", "project"],
        ),
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
    return state, agent_id, issued.raw_token, external_ids


async def _serve(state: DaemonState, port: int) -> asyncio.AbstractServer:
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
    )


async def _stop(server: asyncio.AbstractServer) -> None:
    server.close()
    await server.wait_closed()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_capture_happy_path(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/api/v1/capture",
                json={"content": "via rest", "facet_type": "style"},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        # Lean envelope: dispatcher result lives at the top level.
        assert "external_id" in body
        assert body["facet_type"] == "style"
        assert body["is_duplicate"] is False
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_recall_query_string(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/recall",
                params={"q": "rest sample", "k": "5"},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "matches" in body
        assert isinstance(body["matches"], list)
        assert "warnings" in body
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_stats(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/stats",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "embed_health" in body
        assert "facet_count" in body
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_facets_list_and_show(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            list_resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/facets",
                params={"facet_type": "style"},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            assert list_resp.status_code == 200
            assert "items" in list_resp.json()

            show_resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/facets/{ids[0]}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
            assert show_resp.status_code == 200
            body = show_resp.json()
            assert body["external_id"] == ids[0]
            assert body["facet_type"] == "style"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_forget_via_delete(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"http://127.0.0.1:{port}/api/v1/facets/{ids[1]}",
                params={"reason": "rest test"},
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["external_id"] == ids[1]
        assert body["deleted_at"] is not None
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_missing_bearer_token(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, _raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/api/v1/stats")
        assert resp.status_code == 401
        body = resp.json()
        # Lean error envelope: no top-level ok flag.
        assert "ok" not in body
        assert body["error"]["code"] == "invalid_input"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_invalid_bearer_token(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, _raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/stats",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "scope_denied"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_unknown_route(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/nonexistent",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "unknown_method"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_recall_missing_query(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/recall",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_input"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_origin_blocked(open_vault: VaultConnection, tmp_path: Path) -> None:
    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/v1/stats",
                headers={
                    "Authorization": f"Bearer {raw_token}",
                    "Origin": "https://evil.example.com",
                },
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "scope_denied"
    finally:
        await _stop(server)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rest_405_post_only_on_mcp(open_vault: VaultConnection, tmp_path: Path) -> None:
    """GET /mcp must still be rejected as 405 — only /api/v1/* opens GET."""

    state, _agent_id, raw_token, _ids = await _bootstrap_state(open_vault, tmp_path / "vault.db")
    port = _pick_port()
    server = await _serve(state, port)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        assert resp.status_code == 405
    finally:
        await _stop(server)
