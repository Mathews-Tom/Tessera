"""End-to-end retrieval pipeline against a real vault + fake adapters.

The pipeline orchestrator is exercised with a deterministic fake embedder
and a fake reranker so the test focuses on wiring, budget enforcement,
and the determinism contract, not on provider behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
            facet_type="project",
            content=f"event {i} about retrieval and pipelines",
            source_tool="test",
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
        facet_types=("project",),
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


@dataclass
class _CountingEmbedder:
    """Fake embedder that records each call to :meth:`embed`.

    Used to verify that the pipeline embeds the query text once per
    :func:`recall` invocation, regardless of how many facet types are
    in scope. Before the _gather_dense_by_type refactor, the pipeline
    called :func:`dense.search` per facet type, which embedded the
    same query N times.
    """

    name: ClassVar[str] = "counting"
    model_name: str = "counting-fake"
    dim: int = 8
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import hashlib

        self.calls.append(list(texts))
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([digest[i] / 255.0 for i in range(self.dim)])
        return out

    async def health_check(self) -> None:
        return None


async def _bootstrap_multi_type_vault(
    open_vault: VaultConnection,
) -> tuple[int, PipelineContext, _CountingEmbedder]:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01MULTI', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _CountingEmbedder()
    reranker = _StaticReranker()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=embedder.dim, activate=True
    )
    facet_types = ("identity", "preference", "workflow", "project", "style")
    for ftype in facet_types:
        for i in range(3):
            capture.capture(
                open_vault.connection,
                agent_id=agent_id,
                facet_type=ftype,
                content=f"{ftype} fact {i} about retrieval pipeline",
                source_tool="test",
            )
    await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model.id,
        batch_size=64,
        now_epoch=100,
    )
    # Reset the call log so we only count embed calls made during recall.
    embedder.calls.clear()
    ctx = PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=reranker,
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULTMULTI",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model="length-score",
            mmr_lambda=0.7,
            max_candidates=50,
            retrieval_mode="rerank_only",
        ),
        tool_budget_tokens=400,
        k=10,
        facet_types=facet_types,
    )
    return agent_id, ctx, embedder


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_embeds_query_once_regardless_of_facet_type_count(
    open_vault: VaultConnection,
) -> None:
    _, ctx, embedder = await _bootstrap_multi_type_vault(open_vault)
    assert len(ctx.facet_types) == 5

    await recall(ctx, query_text="retrieval pipeline")

    # The working-set embedding stage runs its own embed pass over
    # candidate content for MMR / SWCR cosine — that call is
    # orthogonal to the dense-fanout fix. The regression-critical
    # invariant is: the *query text* itself is embedded once per
    # recall, not once per facet type. Before the _gather_dense_by_type
    # refactor this was 5 calls for a 5-type recall.
    query_embed_calls = [call for call in embedder.calls if call == ["retrieval pipeline"]]
    assert len(query_embed_calls) == 1, (
        f"expected the query text to be embedded exactly once; got "
        f"{len(query_embed_calls)} calls matching, full log: {embedder.calls!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pipeline_multi_facet_type_recall_surfaces_every_type(
    open_vault: VaultConnection,
) -> None:
    _, ctx, _ = await _bootstrap_multi_type_vault(open_vault)
    result = await recall(ctx, query_text="retrieval pipeline")
    types_seen = {m.facet_type for m in result.matches}
    # With 3 facets per type and k=10, the budget-trimmed top-k should
    # span most or all of the 5 types; require at minimum 3 distinct
    # types to guard against accidental single-type collapse from the
    # refactor.
    assert len(types_seen) >= 3, f"expected ≥3 types in recall; got {types_seen}"
