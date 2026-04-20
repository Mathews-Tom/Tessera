"""End-to-end exercise of the six MCP tools against a real vault.

Uses deterministic fake adapters (sha256-hash embedder, length-inverse
reranker) so the test isolates tool-surface behaviour — validation,
scope enforcement, budget clamping, audit shape — from provider-side
latency. The heavy lifting beneath each tool is covered in its own
phase's test file; here we pin the boundary contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.mcp_surface import tools as mcp
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext
from tessera.retrieval.seed import RetrievalConfig
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


async def _bootstrap(
    open_vault: VaultConnection,
    vault_path: Path,
    *,
    scope_read: Sequence[str] = ("style", "episodic"),
    scope_write: Sequence[str] = ("style", "episodic"),
    style_count: int = 5,
    episodic_count: int = 7,
) -> mcp.ToolContext:
    """Set up an agent, a capability, embedded facets, and a ToolContext."""

    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01MCPSURF', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=_DIM, activate=True
    )
    for i in range(style_count):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="style",
            content=f"voice sample {i}: terse imperative code-first",
            source_client="test",
            captured_at=1_000_000 + i,
        )
    for i in range(episodic_count):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="episodic",
            content=f"event {i}: decided to ship P8 today",
            source_client="test",
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
        scope=build_scope(read=scope_read, write=scope_write),
        now_epoch=1_000_000,
    )
    verified = tokens.verify_and_touch(
        open_vault.connection, raw_token=issued.raw_token, now_epoch=1_000_001
    )
    pipeline = PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=_LengthReranker(),
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULT-MCP",
        agent_id=agent_id,
        config=RetrievalConfig(rerank_model="length", mmr_lambda=0.7, max_candidates=50),
        tool_budget_tokens=6000,
        k=10,
        facet_types=("style", "episodic"),
    )
    return mcp.ToolContext(
        conn=open_vault.connection,
        verified=verified,
        vault_path=vault_path,
        pipeline=pipeline,
        clock=lambda: 1_000_100,
    )


# ---- capture ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_inserts_new_facet(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_write=("style", "episodic", "semantic"),
    )
    resp = await mcp.capture(
        tctx,
        content="freshly captured note",
        facet_type="semantic",
        source_client="cli",
    )
    assert resp.is_duplicate is False
    assert resp.facet_type == "semantic"
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_respects_write_scope(open_vault: VaultConnection, vault_path: Path) -> None:
    # Read-only capability: write MUST be denied, audit MUST record.
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=("style", "episodic", "semantic"),
        scope_write=(),
    )
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.capture(tctx, content="denied", facet_type="style")
    assert exc.value.code == "scope_denied"
    row = open_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op='scope_denied' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_rejects_oversized_content(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="exceeds max"):
        await mcp.capture(tctx, content="x" * 70_000, facet_type="style")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_rejects_unknown_facet_type(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="not in"):
        await mcp.capture(tctx, content="ok", facet_type="bogus")


# ---- recall -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_returns_matches_and_respects_read_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.recall(tctx, query_text="voice sample", k=5)
    assert isinstance(resp, mcp.RecallResponse)
    assert len(resp.matches) > 0
    assert all(m.token_count > 0 for m in resp.matches)
    assert resp.total_tokens <= mcp.RECALL_RESPONSE_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_scope_partial_denial_raises(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    # style scope only; request both → deny before running retrieval.
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style",), scope_write=())
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.recall(tctx, query_text="q", k=5, facet_types=("style", "episodic"))
    assert exc.value.required_facet_type == "episodic"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_rejects_oversized_query(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError):
        await mcp.recall(tctx, query_text="x" * 5_000, k=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_clamps_over_budget_request(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.recall(tctx, query_text="voice", k=5, requested_budget_tokens=50_000)
    # Budget must be clamped to the tool ceiling even if the caller
    # asked for more.
    assert resp.total_tokens <= mcp.RECALL_RESPONSE_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_budget_truncation_flag(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    # Tiny budget: at least some facets must fall outside it.
    resp = await mcp.recall(tctx, query_text="voice", k=10, requested_budget_tokens=5)
    assert resp.truncated is True


# ---- assume_identity ----------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assume_identity_returns_budgeted_bundle(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.assume_identity(tctx)
    assert resp.total_tokens > 0
    assert resp.total_tokens <= mcp.ASSUME_IDENTITY_RESPONSE_BUDGET
    assert "voice" in resp.per_role_counts
    assert "recent_events" in resp.per_role_counts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assume_identity_denied_without_read_on_all_role_facets(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    # Drop episodic read scope — recent_events role's facet_type is
    # episodic, so the call must be denied.
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style",))
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.assume_identity(tctx)
    assert exc.value.required_facet_type == "episodic"


# ---- show ---------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_show_returns_facet_by_external_id(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    resp = await mcp.show(tctx, external_id=external_id)
    assert resp.external_id == external_id
    assert resp.facet_type == "style"
    assert resp.token_count > 0
    assert resp.token_count <= mcp.SHOW_RESPONSE_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_show_rejects_bad_ulid(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="not a valid ULID"):
        await mcp.show(tctx, external_id="not-a-ulid")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_show_rejects_unknown_facet(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="does not exist"):
        await mcp.show(tctx, external_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")


# ---- list_facets -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_facets_returns_summaries(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.list_facets(tctx, facet_type="style", limit=3)
    assert len(resp.items) == 3
    assert all(item.facet_type == "style" for item in resp.items)
    assert resp.total_tokens <= mcp.LIST_FACETS_RESPONSE_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_facets_scope_denial(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style",))
    with pytest.raises(mcp.ScopeDenied):
        await mcp.list_facets(tctx, facet_type="episodic", limit=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_facets_rejects_bad_limit(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError):
        await mcp.list_facets(tctx, facet_type="style", limit=0)
    with pytest.raises(mcp.ValidationError):
        await mcp.list_facets(tctx, facet_type="style", limit=999)


# ---- stats --------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stats_exposes_required_fields(open_vault: VaultConnection, vault_path: Path) -> None:
    """P8 exit gate: stats() includes embed_health, by_source, active_models, vault_size_bytes."""

    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.stats(tctx)
    assert resp.embed_health.embedded > 0
    assert "test" in resp.by_source
    assert len(resp.active_models) == 1
    assert resp.active_models[0].name == "ollama"
    assert resp.vault_size_bytes > 0
    assert resp.facet_count == 12  # 5 style + 7 episodic


# ---- cross-cutting ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_tool_errors_carry_distinct_codes(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    """Codes are stable strings for future JSON-RPC error discrimination."""

    tctx = await _bootstrap(open_vault, vault_path, scope_write=())
    with pytest.raises(mcp.ScopeDenied) as scope_exc:
        await mcp.capture(tctx, content="x", facet_type="style")
    assert scope_exc.value.code == "scope_denied"
    with pytest.raises(mcp.ValidationError) as val_exc:
        await mcp.capture(tctx, content="", facet_type="style")
    assert val_exc.value.code == "invalid_input"
