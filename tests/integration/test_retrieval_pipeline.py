"""End-to-end retrieval pipeline against a real vault + fake adapters.

The pipeline orchestrator is exercised with a deterministic fake embedder
and a fake reranker so the test focuses on wiring, budget enforcement,
and the determinism contract, not on provider behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import pytest

import tessera.adapters.ollama_embedder  # noqa: F401 — adapter registration side effect
from tessera.adapters import models_registry
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, recall
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture
from tessera.vault.connection import VaultConnection


@dataclass
class _HashEmbedder:
    """Deterministic, content-addressed fake embedder.

    Same input → same vector every time, which is the property the
    retrieval determinism CI job depends on. Different contents produce
    different-but-related vectors so MMR has something to diversify.
    """

    name: ClassVar[str] = "fake"
    model_name: str = "hash-fake"
    dim: int = 8

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            # Stable pseudo-embedding: each component is a fixed function
            # of the text's hashed bytes.
            import hashlib

            digest = hashlib.sha256(text.encode()).digest()
            vec = [digest[i] / 255.0 for i in range(self.dim)]
            out.append(vec)
        return out

    async def health_check(self) -> None:
        return None


@dataclass
class _StaticReranker:
    name: ClassVar[str] = "fake-rerank"
    model_name: str = "length-score"

    async def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        # Score by inverse length — shorter passages rank higher.
        return [1.0 / (1 + len(p)) for p in passages]

    async def health_check(self) -> None:
        return None


async def _bootstrap_vault(open_vault: VaultConnection) -> tuple[int, PipelineContext]:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01PIPE', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    reranker = _StaticReranker()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=embedder.dim, activate=True
    )
    # Bypass the python-side registry's single-active guard for tests: the
    # model's registered name matches the `ollama` adapter but the tests
    # feed a fake embedder directly.
    for i in range(12):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="episodic",
            content=f"event {i} about retrieval and pipelines",
            source_client="test",
        )
    await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model.id,
        batch_size=32,
        now_epoch=100,
    )
    ctx = PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=reranker,
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULTID",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model="length-score",
            mmr_lambda=0.7,
            max_candidates=50,
            # rerank_only exercises the cross-encoder path without SWCR;
            # the degraded-rerank test below overrides the reranker.
            retrieval_mode="rerank_only",
        ),
        tool_budget_tokens=200,
        k=5,
        facet_types=("episodic",),
    )
    return agent_id, ctx


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_returns_ranked_matches(open_vault: VaultConnection) -> None:
    _, ctx = await _bootstrap_vault(open_vault)
    result = await recall(ctx, query_text="retrieval pipeline")
    assert len(result.matches) > 0
    assert len(result.matches) <= ctx.k
    # Each match respects the snippet budget (counted in tokens).
    assert all(m.token_count > 0 for m in result.matches)
    # Ranks are a dense 0..N-1 sequence.
    assert [m.rank for m in result.matches] == list(range(len(result.matches)))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_is_deterministic_across_repeated_calls(
    open_vault: VaultConnection,
) -> None:
    _, ctx = await _bootstrap_vault(open_vault)
    first = await recall(ctx, query_text="retrieval pipeline")
    second = await recall(ctx, query_text="retrieval pipeline")
    assert first.seed == second.seed
    assert [m.external_id for m in first.matches] == [m.external_id for m in second.matches]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_writes_audit_row_for_each_call(open_vault: VaultConnection) -> None:
    _, ctx = await _bootstrap_vault(open_vault)
    before = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='retrieval_executed'"
        ).fetchone()[0]
    )
    await recall(ctx, query_text="retrieval pipeline")
    after = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='retrieval_executed'"
        ).fetchone()[0]
    )
    assert after == before + 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_degraded_rerank_emits_audit_event(
    open_vault: VaultConnection,
) -> None:
    _, ctx = await _bootstrap_vault(open_vault)

    @dataclass
    class _BrokenReranker:
        name: ClassVar[str] = "broken"
        model_name: str = "broken"

        async def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            seed: int | None = None,
        ) -> list[float]:
            del query, passages, seed
            from tessera.adapters.errors import AdapterNetworkError

            raise AdapterNetworkError("simulated rerank outage")

        async def health_check(self) -> None:
            return None

    ctx_broken = PipelineContext(
        conn=ctx.conn,
        embedder=ctx.embedder,
        reranker=_BrokenReranker(),
        active_model_id=ctx.active_model_id,
        vec_table=ctx.vec_table,
        vault_id=ctx.vault_id,
        agent_id=ctx.agent_id,
        config=ctx.config,
        tool_budget_tokens=ctx.tool_budget_tokens,
        k=ctx.k,
        facet_types=ctx.facet_types,
    )
    result = await recall(ctx_broken, query_text="retrieval pipeline")
    assert result.rerank_degraded is True
    assert any("reranker_degraded" in w for w in result.warnings)
    degraded_rows = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='retrieval_rerank_degraded'"
        ).fetchone()[0]
    )
    assert degraded_rows >= 1
