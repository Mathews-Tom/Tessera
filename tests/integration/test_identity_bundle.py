"""End-to-end assume_identity bundle against a real vault + fake adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import pytest

import tessera.adapters.ollama_embedder  # noqa: F401 — registration side effect
from tessera.adapters import models_registry
from tessera.identity.bundle import assume_identity
from tessera.identity.roles import DEFAULT_ROLES, RoleSpec
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import PipelineContext
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import capture
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
    open_vault: VaultConnection, *, style_count: int = 8, episodic_count: int = 15
) -> PipelineContext:
    cur = open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01BUNDLE', 'a', 0)"
    )
    agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(
        open_vault.connection, name="ollama", dim=_DIM, activate=True
    )
    # Fresh episodic facets captured in the last hour; style facets are
    # undated (time_window_hours is None for the voice role).
    for i in range(style_count):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="style",
            content=f"voice sample {i}: terse, imperative, code-first",
            source_client="test",
            captured_at=1_000_000 + i,
        )
    for i in range(episodic_count):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="episodic",
            content=f"event {i}: decided to ship P6 today",
            source_client="test",
            captured_at=1_000_000 + i,
        )
    while True:
        stats = await embed_worker.run_pass(
            open_vault.connection, embedder, active_model_id=model.id, batch_size=32
        )
        if stats.embedded == 0:
            break
    return PipelineContext(
        conn=open_vault.connection,
        embedder=embedder,
        reranker=_LengthReranker(),
        active_model_id=model.id,
        vec_table=models_registry.vec_table_name(model.id),
        vault_id="01VAULTBUNDLE",
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model="length",
            mmr_lambda=0.7,
            max_candidates=50,
        ),
        tool_budget_tokens=6000,  # bundle budget, not per-role
        k=20,  # overridden per role by the assembler
        facet_types=("style", "episodic"),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_returns_per_role_facets(open_vault: VaultConnection) -> None:
    ctx = await _bootstrap(open_vault)
    bundle = await assume_identity(ctx, now_epoch=1_000_000 + 100)
    assert bundle.total_tokens > 0
    assert bundle.total_tokens <= bundle.total_budget_tokens
    # Both active roles should have at least one facet under the 6K budget.
    assert "voice" in bundle.per_role
    assert "recent_events" in bundle.per_role
    assert len(bundle.per_role["voice"]) >= 1
    assert len(bundle.per_role["recent_events"]) >= 1
    # Skills / relationships / goals are v0.3+ types; not in per_role.
    assert "skills" not in bundle.per_role
    assert "relationships" not in bundle.per_role
    assert "goals" not in bundle.per_role


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_respects_time_window_on_recent_events(
    open_vault: VaultConnection,
) -> None:
    ctx = await _bootstrap(open_vault)
    # now far in the future of captured_at; everything is older than the
    # window and recent_events should return empty while voice remains.
    bundle = await assume_identity(
        ctx,
        now_epoch=10_000_000,  # well past all captured_at=1_000_000+i entries
        recent_window_hours=1,
    )
    assert bundle.per_role["recent_events"] == ()
    assert len(bundle.per_role["voice"]) >= 1
    # A warning is emitted for the empty role because k_min=5 was unmet.
    assert any("recent_events" in w for w in bundle.warnings)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_is_deterministic_across_repeated_calls(
    open_vault: VaultConnection,
) -> None:
    ctx = await _bootstrap(open_vault)
    first = await assume_identity(ctx, now_epoch=1_000_000 + 100)
    second = await assume_identity(ctx, now_epoch=1_000_000 + 100)
    assert first.seed == second.seed
    assert [f.external_id for f in first.facets] == [f.external_id for f in second.facets]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_writes_audit_row(open_vault: VaultConnection) -> None:
    ctx = await _bootstrap(open_vault)
    before = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='identity_bundle_assembled'"
        ).fetchone()[0]
    )
    await assume_identity(ctx, now_epoch=1_000_000 + 100)
    after = int(
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='identity_bundle_assembled'"
        ).fetchone()[0]
    )
    assert after == before + 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_explain_mode_emits_reason_strings(
    open_vault: VaultConnection,
) -> None:
    ctx = await _bootstrap(open_vault)
    bundle = await assume_identity(ctx, now_epoch=1_000_000 + 100, explain=True)
    for facet in bundle.facets:
        assert facet.reason is not None
        assert "role=" in facet.reason
        assert "rank=" in facet.reason
        assert "score=" in facet.reason


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_default_explain_omits_reason(open_vault: VaultConnection) -> None:
    ctx = await _bootstrap(open_vault)
    bundle = await assume_identity(ctx, now_epoch=1_000_000 + 100)
    for facet in bundle.facets:
        assert facet.reason is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_rejects_invalid_budget(open_vault: VaultConnection) -> None:
    ctx = await _bootstrap(open_vault)
    with pytest.raises(ValueError, match="total_budget_tokens"):
        await assume_identity(ctx, total_budget_tokens=0)
    with pytest.raises(ValueError, match="recent_window_hours"):
        await assume_identity(ctx, recent_window_hours=0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_with_custom_role_list_only_uses_active(
    open_vault: VaultConnection,
) -> None:
    ctx = await _bootstrap(open_vault)
    # Pass only the skills role, which has no matching facet_type in v0.1.
    # The assembler drops it and raises because no active role remains.
    skills_role = next(r for r in DEFAULT_ROLES if r.name == "skills")
    with pytest.raises(ValueError, match="no active roles"):
        await assume_identity(
            ctx,
            roles=(skills_role,),
            now_epoch=1_000_000 + 100,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_total_tokens_match_sum_of_facet_tokens(
    open_vault: VaultConnection,
) -> None:
    ctx = await _bootstrap(open_vault)
    bundle = await assume_identity(ctx, now_epoch=1_000_000 + 100)
    assert bundle.total_tokens == sum(f.token_count for f in bundle.facets)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bundle_custom_role_with_single_active(open_vault: VaultConnection) -> None:
    ctx = await _bootstrap(open_vault)
    voice_only = (
        RoleSpec(
            name="voice",
            facet_type="style",
            budget_fraction=1.0,
            k_min=1,
            k_max=5,
        ),
    )
    bundle = await assume_identity(ctx, roles=voice_only, now_epoch=1_000_000 + 100)
    assert set(bundle.per_role.keys()) == {"voice"}
    assert all(f.role == "voice" for f in bundle.facets)
