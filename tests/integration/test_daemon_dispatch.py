"""Method-name → tool dispatcher: every method, every edge."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon.dispatch import UnknownMethodError, dispatch_tool_call
from tessera.daemon.state import DaemonState
from tessera.mcp_surface import tools as mcp
from tessera.retrieval import embed_worker
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection


@dataclass
class _HashEmbedder:
    name: ClassVar[str] = "ollama"
    model_name: str = "hash-fake"
    dim: int = 8

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [hashlib.sha256(t.encode()).digest()[i] / 255.0 for i in range(self.dim)] for t in texts
        ]

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


async def _bootstrap(
    open_vault: VaultConnection, vault_path: Path
) -> tuple[DaemonState, tokens.VerifiedCapability]:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01DISP', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=8, activate=True
    )
    for i in range(4):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="style",
            content=f"voice sample {i}",
            source_tool="t",
            captured_at=1_000_000 + i,
        )
    for i in range(4):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="project",
            content=f"project note {i}",
            source_tool="t",
            captured_at=1_000_000 + i,
        )
    while True:
        stats = await embed_worker.run_pass(
            open_vault.connection, embedder, active_model_id=model.id, batch_size=32
        )
        if stats.embedded == 0:
            break
    now = int(datetime.now(UTC).timestamp())
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style", "project"], write=["style", "project"]),
        now_epoch=now,
    )
    verified = tokens.verify_and_touch(
        open_vault.connection, raw_token=issued.raw_token, now_epoch=now
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
    return state, verified


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_unknown_method_raises(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    with pytest.raises(UnknownMethodError, match="bogus"):
        await dispatch_tool_call(state, verified, "bogus", {})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_capture(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(
        state,
        verified,
        "capture",
        {"content": "new project note", "facet_type": "project"},
    )
    assert result["is_duplicate"] is False
    assert len(result["external_id"]) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_capture_missing_content(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError):
        await dispatch_tool_call(state, verified, "capture", {"facet_type": "style"})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_recall(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(state, verified, "recall", {"query_text": "voice", "k": 3})
    assert "matches" in result
    assert "seed" in result
    # V0.5-P7: every match dict carries mode + is_stale on the wire.
    # Non-compiled facet types default to query_time / False; the
    # MCP-layer integration suite covers the write_time path. This
    # assertion pins the dispatch JSON shape so a regression that
    # drops either key from ``_match_to_json`` fails here.
    for match in result["matches"]:
        assert "mode" in match
        assert "is_stale" in match
        assert match["mode"] == "query_time"
        assert match["is_stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_recall_uses_contract_default_k(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(state, verified, "recall", {"query_text": "voice"})
    assert "matches" in result
    assert len(result["matches"]) <= 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_recall_with_facet_types_list(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(
        state,
        verified,
        "recall",
        {"query_text": "voice", "k": 3, "facet_types": ["style"]},
    )
    assert all(m["facet_type"] == "style" for m in result["matches"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_recall_rejects_non_int_k(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError):
        await dispatch_tool_call(state, verified, "recall", {"query_text": "q", "k": "three"})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_show(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    result = await dispatch_tool_call(state, verified, "show", {"external_id": external_id})
    assert result["external_id"] == external_id
    assert result["token_count"] > 0
    assert result["source_tool"] == "t"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_list_facets(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(
        state, verified, "list_facets", {"facet_type": "style", "limit": 2}
    )
    assert len(result["items"]) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_stats(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    result = await dispatch_tool_call(state, verified, "stats", {})
    assert result["facet_count"] == 8
    assert result["embed_health"]["embedded"] == 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_forget(open_vault: VaultConnection, vault_path: Path) -> None:
    state, verified = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    result = await dispatch_tool_call(
        state,
        verified,
        "forget",
        {"external_id": external_id, "reason": "retire example"},
    )
    assert result["external_id"] == external_id
    assert result["facet_type"] == "style"
    assert result["deleted_at"] > 0
    # The second forget is a validation error: the facet is already soft-deleted.
    with pytest.raises(mcp.ValidationError):
        await dispatch_tool_call(
            state,
            verified,
            "forget",
            {"external_id": external_id},
        )
