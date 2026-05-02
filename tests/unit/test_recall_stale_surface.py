"""V0.5-P7 recall surfaces compiled-artifact mode + staleness.

ADR 0019 §Retrieval surface commits two metadata fields on every
``recall`` match: ``mode`` (the row's production method) and
``is_stale`` (only meaningful for ``compiled_notebook`` rows but
present uniformly so callers do not need facet-type-specific
branches).

The unit suite covers four invariants:

1. **Hydration shape** — ``_hydrate_match_metadata`` returns the
   right tuple for compiled-notebook and non-compiled facet ids.
2. **Pipeline propagation** — running a recall over a vault
   containing a compiled artifact surfaces ``mode='write_time'``
   on the corresponding match.
3. **Staleness propagation** — after a source mutation flips the
   artifact's ``is_stale`` flag (V0.5-P6 wiring), the next recall
   surfaces ``is_stale=True``.
4. **Default for non-compiled types** — every other facet type
   surfaces ``mode='query_time'`` and ``is_stale=False`` uniformly.

The pipeline is exercised through the same in-process fake-adapter
fixtures the existing retrieval tests use; this avoids spinning up
the daemon while still covering the storage hydration query end-
to-end.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import pytest
import sqlcipher3

from tessera.adapters import models_registry
from tessera.retrieval import embed_worker
from tessera.retrieval.pipeline import (
    PipelineContext,
    _hydrate_match_metadata,
    recall,
)
from tessera.retrieval.seed import RetrievalConfig
from tessera.vault import (
    agent_profiles,
    capture,
    compiled,
    facets,
    skills,
)
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
        self,
        query: str,
        passages: Sequence[str],
        *,
        seed: int | None = None,
    ) -> list[float]:
        del query, seed
        return [1.0 / (1 + len(p)) for p in passages]

    async def health_check(self) -> None:
        return None


def _seed_agent(conn: sqlcipher3.Connection) -> int:
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01RECALLP7', 'a', 0)")
    return int(conn.execute("SELECT id FROM agents WHERE external_id='01RECALLP7'").fetchone()[0])


def _seed_profile(conn: sqlcipher3.Connection, agent_id: int) -> str:
    external_id, _ = agent_profiles.register(
        conn,
        agent_id=agent_id,
        content="profile body for V0.5-P7 recall test",
        metadata={
            "purpose": "summarise standups",
            "inputs": ["notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    return external_id


def _seed_skill(conn: sqlcipher3.Connection, agent_id: int) -> str:
    external_id, _ = skills.create_skill(
        conn,
        agent_id=agent_id,
        name="git-rebase-recall-test",
        description="Squash branches before merge",
        procedure_md="# Procedure\n\nUse interactive rebase per the recall test.",
        source_tool="cli",
    )
    return external_id


async def _bootstrap_pipeline(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_types: tuple[str, ...] = (
        "agent_profile",
        "compiled_notebook",
        "project",
        "skill",
        "style",
    ),
) -> PipelineContext:
    embedder = _HashEmbedder()
    model = models_registry.register_embedding_model(conn, name="ollama", dim=_DIM, activate=True)
    while True:
        stats = await embed_worker.run_pass(conn, embedder, active_model_id=model.id, batch_size=32)
        if stats.embedded == 0:
            break
    return PipelineContext(
        conn=conn,
        embedder=embedder,
        reranker=_LengthReranker(),
        active_model_id=model.id,
        vec_table=f"vec_{model.id}",
        vault_id="01TESTVAULT",
        agent_id=agent_id,
        config=RetrievalConfig(rerank_model="length", mmr_lambda=0.7, max_candidates=50),
        tool_budget_tokens=4_000,
        k=10,
        facet_types=facet_types,
    )


def _register_playbook(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    sources: list[str],
) -> str:
    return compiled.register_compiled_artifact(
        conn,
        agent_id=agent_id,
        content="The Playbook narrative for V0.5-P7 recall surface tests.",
        source_facets=sources,
        compiler_version="claude-opus-4-7",
        source_tool="cli",
    )


# ---- _hydrate_match_metadata --------------------------------------------


@pytest.mark.unit
def test_hydrate_returns_query_time_for_non_compiled_facet(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    cap = capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="A project facet for hydration test.",
        source_tool="cli",
    )
    facet_id = int(
        conn.execute(
            "SELECT id FROM facets WHERE external_id = ?",
            (cap.external_id,),
        ).fetchone()[0]
    )
    enriched = _hydrate_match_metadata(conn, [facet_id])
    assert facet_id in enriched
    captured_at, mode, is_stale = enriched[facet_id]
    assert captured_at > 0
    assert mode == "query_time"
    assert is_stale is False


@pytest.mark.unit
def test_hydrate_returns_write_time_and_stale_flag_for_compiled_notebook(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])
    facet_id = int(
        conn.execute(
            "SELECT id FROM facets WHERE external_id = ?",
            (artifact_id,),
        ).fetchone()[0]
    )

    # Fresh artifact: is_stale=False.
    enriched = _hydrate_match_metadata(conn, [facet_id])
    captured_at, mode, is_stale = enriched[facet_id]
    assert captured_at > 0
    assert mode == "write_time"
    assert is_stale is False

    # After a source mutation flips the artifact stale, hydration
    # surfaces is_stale=True.
    facets.soft_delete(conn, profile_id)
    enriched_after = _hydrate_match_metadata(conn, [facet_id])
    _, mode_after, is_stale_after = enriched_after[facet_id]
    assert mode_after == "write_time"
    assert is_stale_after is True


@pytest.mark.unit
def test_hydrate_returns_empty_for_empty_id_list(
    open_vault: VaultConnection,
) -> None:
    """Defensive boundary: passing an empty list should not produce a
    malformed SQL placeholder string. The caller in ``_to_matches``
    short-circuits before this branch, but the helper should still
    behave sanely."""

    conn = open_vault.connection
    _seed_agent(conn)
    enriched = _hydrate_match_metadata(conn, [])
    assert enriched == {}


@pytest.mark.unit
def test_hydrate_skips_unknown_facet_id(
    open_vault: VaultConnection,
) -> None:
    """An id that does not match any row should be absent from the
    result dict, not surfaced with synthetic defaults. Callers
    handle the missing-key case via ``dict.get(..., default)``."""

    conn = open_vault.connection
    _seed_agent(conn)
    enriched = _hydrate_match_metadata(conn, [99_999])
    assert enriched == {}


# ---- end-to-end recall propagation --------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_surfaces_write_time_mode_on_compiled_notebook(
    open_vault: VaultConnection,
) -> None:
    """A bare ``recall`` over a vault that contains a Playbook
    surfaces the artifact with ``mode='write_time'`` so callers can
    render it differently from raw query-time facets."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])
    ctx = await _bootstrap_pipeline(conn, agent_id=agent_id)

    result = await recall(ctx, query_text="Playbook narrative recall surface")

    by_external_id = {m.external_id: m for m in result.matches}
    assert artifact_id in by_external_id
    assert by_external_id[artifact_id].mode == "write_time"
    assert by_external_id[artifact_id].is_stale is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_surfaces_is_stale_after_source_mutation(
    open_vault: VaultConnection,
) -> None:
    """V0.5-P6 wiring flips ``is_stale=1`` when a source mutates;
    V0.5-P7 wiring surfaces that flag on the next recall. Together
    they let a caller learn the artifact is out of date without an
    explicit re-fetch."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    skill_id = _seed_skill(conn, agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[skill_id])
    ctx = await _bootstrap_pipeline(conn, agent_id=agent_id)

    skills.update_procedure(
        conn,
        external_id=skill_id,
        procedure_md="# Procedure\n\nUpdated rebase recipe.",
    )

    result = await recall(ctx, query_text="Playbook narrative recall surface")

    by_external_id = {m.external_id: m for m in result.matches}
    assert by_external_id[artifact_id].is_stale is True
    assert by_external_id[artifact_id].mode == "write_time"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_returns_query_time_mode_for_non_compiled_matches(
    open_vault: VaultConnection,
) -> None:
    """Every match for a non-``compiled_notebook`` facet type
    surfaces ``mode='query_time'`` and ``is_stale=False``. Uniform
    field shape — no nullable surface."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="A project facet that should surface as query_time.",
        source_tool="cli",
    )
    capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="style",
        content="A style facet that should surface as query_time.",
        source_tool="cli",
    )
    ctx = await _bootstrap_pipeline(conn, agent_id=agent_id, facet_types=("project", "style"))

    result = await recall(ctx, query_text="surface query time")

    assert result.matches
    for match in result.matches:
        assert match.mode == "query_time"
        assert match.is_stale is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_excludes_soft_deleted_compiled_notebook(
    open_vault: VaultConnection,
) -> None:
    """A soft-deleted ``compiled_notebook`` facet must not surface
    via recall. The V0.5-P6 JOIN-based tombstone filter on
    ``compiled.get`` covers the storage layer; this pins the
    end-to-end recall surface."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])
    facets.soft_delete(conn, artifact_id)
    ctx = await _bootstrap_pipeline(conn, agent_id=agent_id)

    result = await recall(ctx, query_text="Playbook narrative recall surface")

    external_ids = {m.external_id for m in result.matches}
    assert artifact_id not in external_ids
