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

import tessera.adapters.fastembed_embedder  # noqa: F401 — adapter registration side effect
from tessera.adapters import models_registry
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext, recall
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture, compiled
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_swcr_augments_with_recent_retrospectives(
    open_vault: VaultConnection,
) -> None:
    """ADR 0018 retrospective integration: when an agent_profile facet
    enters the SWCR working set, the most recent retrospectives whose
    ``agent_ref`` matches that profile join the candidate graph."""

    from dataclasses import replace

    from tessera.vault import agent_profiles, retrospectives

    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01SWCR', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _CountingEmbedder()
    reranker = _StaticReranker()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=embedder.dim, activate=True
    )
    profile_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="weekly digest agent profile",
        metadata={
            "purpose": "summarize standups",
            "inputs": ["standup notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    for i in range(3):
        retrospectives.record(
            open_vault.connection,
            agent_id=agent_id,
            content=f"retrospective body {i}",
            metadata={
                "agent_ref": profile_id,
                "task_id": f"task-{i}",
                "went_well": ["captured digest"],
                "gaps": ["missed migration risk"],
                "changes": [
                    {"target": "verification_checklist", "change": "Add ALTER TABLE scan"},
                ],
                "outcome": "partial",
            },
            source_tool="cli",
        )
    await embed_worker.run_pass(
        open_vault.connection,
        embedder,
        active_model_id=model.id,
        batch_size=64,
        now_epoch=100,
    )
    ctx = PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=reranker,
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULTSWCR",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model="length-score",
            mmr_lambda=0.7,
            max_candidates=50,
        ),
        tool_budget_tokens=4000,
        k=10,
        facet_types=("agent_profile", "retrospective"),
    )
    result = await recall(ctx, query_text="weekly digest agent")
    types_seen = {m.facet_type for m in result.matches}
    # Augmentation surfaces retrospectives in the SWCR working set;
    # the bundle should carry both the profile and at least one
    # retrospective row.
    assert "agent_profile" in types_seen
    assert "retrospective" in types_seen

    # Disabling the window (window=0) collapses the augmentation;
    # only the agent_profile row remains in scope, retrospectives
    # surface only via direct hybrid retrieval (which they may or
    # may not, depending on the query text — they certainly do not
    # surface as augmentations).
    no_aug_config = replace(ctx.config, retrospective_window=0)
    no_aug_ctx = replace(ctx, config=no_aug_config)
    no_aug_result = await recall(no_aug_ctx, query_text="weekly digest agent")
    aug_retro_count = sum(1 for m in result.matches if m.facet_type == "retrospective")
    no_aug_retro_count = sum(1 for m in no_aug_result.matches if m.facet_type == "retrospective")
    assert aug_retro_count >= no_aug_retro_count


# ---- V0.5 Playbook retrieval and staleness contract ---------------------
#
# Phase 4 of the compiled-Playbooks enhancement plan promises four
# observable invariants on the recall surface:
#
# 1. A fresh ``compiled_notebook`` row may surface like any other
#    candidate, carrying ``mode='write_time'`` and ``is_stale=False``.
# 2. A stale ``compiled_notebook`` row may still surface when relevant
#    but must carry ``is_stale=True``, and the bundle must emit a
#    bundle-level ``compiled_artifact_stale`` warning so a caller
#    scanning warnings (audit triage, dashboards) cannot miss the
#    fact that a non-authoritative row is in the response.
# 3. A stale row never surfaces with ``is_stale=False`` — the flag
#    travels from ``compiled_artifacts.is_stale`` to the match through
#    the hydration LEFT JOIN; no code path reaches a stale artifact
#    and presents it as fresh.
# 4. A ``write_time`` facet without a paired ``compiled_artifacts``
#    row violates ADR 0019's pair-write contract; rather than
#    surface a fabricated "fresh authoritative brief" with
#    ``is_stale=False``, the pipeline raises so the outer recall
#    handler records the integrity breach (caller-visible failure,
#    not silent fallback to raw recall).


async def _bootstrap_playbook_vault(
    open_vault: VaultConnection,
) -> tuple[int, PipelineContext, str, str]:
    """Seed an agent with one source facet plus a compiled Playbook.

    Returns ``(agent_id, ctx, source_external_id, artifact_external_id)``
    so individual tests can mutate the source to flip the artifact
    stale.
    """

    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01PLAY', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    reranker = _StaticReranker()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=embedder.dim, activate=True
    )
    source = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="release prep workflow notes for the playbook compiler",
        source_tool="test",
    )
    artifact_external_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content=("Playbook: release prep workflow with gates, evidence, and handoff state"),
        source_facets=[source.external_id],
        compiler_version="test/manual@1",
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
        vault_id="01VAULTPLAY",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model="length-score",
            mmr_lambda=0.7,
            max_candidates=50,
            retrieval_mode="rerank_only",
        ),
        tool_budget_tokens=2000,
        k=10,
        facet_types=("project", "compiled_notebook"),
    )
    return agent_id, ctx, source.external_id, artifact_external_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_surfaces_fresh_playbook_with_write_time_mode(
    open_vault: VaultConnection,
) -> None:
    _, ctx, _, artifact_id = await _bootstrap_playbook_vault(open_vault)
    result = await recall(ctx, query_text="release prep workflow playbook")
    playbooks = [m for m in result.matches if m.facet_type == "compiled_notebook"]
    assert playbooks, "expected fresh compiled_notebook row in recall bundle"
    fresh = next(m for m in playbooks if m.external_id == artifact_id)
    assert fresh.mode == "write_time"
    assert fresh.is_stale is False
    # Bundle carries no stale-artifact warning when nothing is stale.
    assert not any("compiled_artifact_stale" in w for w in result.warnings)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_surfaces_stale_playbook_with_warning(
    open_vault: VaultConnection,
) -> None:
    agent_id, ctx, source_id, artifact_id = await _bootstrap_playbook_vault(open_vault)
    flipped = compiled.mark_stale_for_source(
        open_vault.connection,
        source_external_id=source_id,
        source_op="facet_soft_deleted",
        agent_id=agent_id,
    )
    assert flipped == 1
    result = await recall(ctx, query_text="release prep workflow playbook")
    playbooks = [m for m in result.matches if m.external_id == artifact_id]
    assert playbooks, "stale Playbook must remain inspectable through recall"
    stale = playbooks[0]
    assert stale.mode == "write_time"
    assert stale.is_stale is True
    # Loud bundle-level signal so a caller scanning warnings cannot
    # mistake the row for authoritative context.
    assert any("compiled_artifact_stale" in w for w in result.warnings), (
        f"expected compiled_artifact_stale warning, got {result.warnings!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_never_surfaces_stale_playbook_as_fresh(
    open_vault: VaultConnection,
) -> None:
    """A stale artifact's ``is_stale`` must travel from storage to match.

    A regression that drops the LEFT JOIN against ``compiled_artifacts``
    or fabricates ``is_stale=False`` for ``write_time`` rows would
    quietly present non-authoritative context as fresh. Guard the
    invariant directly.
    """

    agent_id, ctx, source_id, artifact_id = await _bootstrap_playbook_vault(open_vault)
    compiled.mark_stale_for_source(
        open_vault.connection,
        source_external_id=source_id,
        source_op="facet_soft_deleted",
        agent_id=agent_id,
    )
    result = await recall(ctx, query_text="release prep workflow playbook")
    for match in result.matches:
        if match.external_id == artifact_id:
            assert match.is_stale is True, (
                "stale compiled artifact must never surface with is_stale=False"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_orphan_write_time_facet_breaches_pair_write_contract(
    open_vault: VaultConnection,
) -> None:
    """ADR 0019 §pair-write: a ``write_time`` facet without a paired
    ``compiled_artifacts`` row is an integrity violation. The hydration
    LEFT JOIN refuses to fabricate an ``is_stale=False`` answer for
    such a row; the pipeline raises so the outer ``recall`` handler
    records the breach in the audit log instead of silently surfacing
    the orphan as fresh authoritative content.
    """

    _, ctx, _, artifact_id = await _bootstrap_playbook_vault(open_vault)
    # Drop the paired artifact row directly to forge the orphan state
    # the contract refuses. Going through ``compiled.forget`` would
    # also tombstone the facet via the shared ``is_deleted`` column,
    # which would mask the orphan rather than expose it.
    open_vault.connection.execute(
        "DELETE FROM compiled_artifacts WHERE external_id = ?",
        (artifact_id,),
    )
    with pytest.raises(RuntimeError, match="recall_hydration_orphan"):
        await recall(ctx, query_text="release prep workflow playbook")
