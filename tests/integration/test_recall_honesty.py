"""Honesty invariant for empty or low-signal recall responses."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

import tessera.adapters.fastembed_embedder  # noqa: F401 — adapter registration side effect
from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.mcp_surface import tools as mcp
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, RecallDegradedReason
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture as vault_capture
from tessera.vault.connection import VaultConnection

_DIM = 8
_FACET_TYPES = ("identity", "preference", "workflow", "project", "style")


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
class _PositiveReranker:
    name: ClassVar[str] = "positive"
    model_name: str = "positive"

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        return [1.0 / (idx + 1) for idx, _passage in enumerate(passages)]

    async def health_check(self) -> None:
        return None


@dataclass
class _ZeroSignalReranker:
    name: ClassVar[str] = "zero-signal"
    model_name: str = "zero-signal"

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        return [0.0 for _ in passages]

    async def health_check(self) -> None:
        return None


async def _tool_context(
    open_vault: VaultConnection,
    vault_path: Path,
    *,
    reranker: _PositiveReranker | _ZeroSignalReranker,
) -> mcp.ToolContext:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01HONESTY', 'honesty', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=_DIM, activate=True
    )
    issued = tokens.issue(
        open_vault.connection,
        agent_id=agent_id,
        client_name="honesty-test",
        token_class="session",
        scope=build_scope(read=_FACET_TYPES, write=_FACET_TYPES),
        now_epoch=1_000_000,
    )
    verified = tokens.verify_and_touch(
        open_vault.connection, raw_token=issued.raw_token, now_epoch=1_000_001
    )
    pipeline = PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=reranker,
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULT-HONESTY",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model=reranker.model_name,
            mmr_lambda=0.7,
            max_candidates=50,
            retrieval_mode="rerank_only",
        ),
        tool_budget_tokens=mcp.RECALL_RESPONSE_BUDGET,
        k=10,
        facet_types=_FACET_TYPES,
    )
    return mcp.ToolContext(
        conn=open_vault.connection,
        verified=verified,
        vault_path=vault_path,
        pipeline=pipeline,
        clock=lambda: 1_000_100,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_empty_vault_returns_empty_bundle_with_degraded_reason(
    open_vault: VaultConnection,
    vault_path: Path,
) -> None:
    tctx = await _tool_context(open_vault, vault_path, reranker=_PositiveReranker())

    resp = await mcp.recall(tctx, query_text="anything", k=5)

    assert resp.matches == ()
    assert resp.degraded_reason is RecallDegradedReason.EMPTY_VAULT
    assert resp.total_tokens == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_below_relevance_floor_returns_empty_with_reason(
    open_vault: VaultConnection,
    vault_path: Path,
) -> None:
    tctx = await _tool_context(open_vault, vault_path, reranker=_ZeroSignalReranker())
    assert tctx.pipeline is not None
    for idx, facet_type in enumerate(_FACET_TYPES):
        vault_capture.capture(
            open_vault.connection,
            agent_id=tctx.pipeline.agent_id,
            facet_type=facet_type,
            content=f"low signal fixture {idx} unrelated to the query",
            source_tool="test",
            captured_at=1_000_000 + idx,
        )
    while True:
        stats = await embed_worker.run_pass(
            open_vault.connection,
            tctx.pipeline.embedder,
            active_model_id=tctx.pipeline.active_model_id,
            batch_size=32,
        )
        if stats.embedded == 0:
            break

    resp = await mcp.recall(tctx, query_text="query that should not be padded", k=5)

    assert resp.matches == ()
    assert resp.degraded_reason is RecallDegradedReason.NO_SIGNAL_ABOVE_FLOOR
    assert resp.total_tokens == 0
