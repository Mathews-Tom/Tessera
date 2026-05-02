"""End-to-end exercise of the six MCP tools against a real vault.

Uses deterministic fake adapters (sha256-hash embedder, length-inverse
reranker) so the test isolates tool-surface behaviour — validation,
scope enforcement, budget clamping, audit shape — from provider-side
latency. The heavy lifting beneath each tool is covered in its own
module's test file; here we pin the boundary contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

import tessera.adapters.fastembed_embedder  # noqa: F401 — registration side effect
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
    scope_read: Sequence[str] = ("style", "project"),
    scope_write: Sequence[str] = ("style", "project"),
    style_count: int = 5,
    project_count: int = 7,
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
            source_tool="test",
            captured_at=1_000_000 + i,
        )
    for i in range(project_count):
        vault_capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="project",
            content=f"project note {i}: shipping P10 reframe reconciliation",
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
        facet_types=("style", "project"),
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
        scope_write=("style", "project", "preference"),
    )
    resp = await mcp.capture(
        tctx,
        content="freshly captured note",
        facet_type="preference",
        source_tool="cli",
    )
    assert resp.is_duplicate is False
    assert resp.facet_type == "preference"
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_respects_write_scope(open_vault: VaultConnection, vault_path: Path) -> None:
    # Read-only capability: write MUST be denied, audit MUST record.
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=("style", "project", "preference"),
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
        await mcp.recall(tctx, query_text="q", k=5, facet_types=("style", "project"))
    assert exc.value.required_facet_type == "project"


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
    assert resp.source_tool == "test"
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
    assert all(item.source_tool == "test" for item in resp.items)
    assert resp.total_tokens <= mcp.LIST_FACETS_RESPONSE_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_facets_scope_denial(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style",))
    with pytest.raises(mcp.ScopeDenied):
        await mcp.list_facets(tctx, facet_type="project", limit=5)


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
    """stats() includes embed_health, by_source, active_models, vault_size_bytes."""

    tctx = await _bootstrap(open_vault, vault_path)
    resp = await mcp.stats(tctx)
    assert resp.embed_health.embedded > 0
    assert "test" in resp.by_source
    assert len(resp.active_models) == 1
    assert resp.active_models[0].name == "ollama"
    assert resp.vault_size_bytes > 0
    assert resp.facet_count == 12  # 5 style + 7 project


# ---- forget -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_soft_deletes_and_audits(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    resp = await mcp.forget(tctx, external_id=external_id, reason="demo")
    assert resp.external_id == external_id
    assert resp.facet_type == "style"
    assert resp.deleted_at == 1_000_100
    # Audit row was written with the target_external_id and the right op.
    row = open_vault.connection.execute(
        "SELECT op, target_external_id, payload FROM audit_log "
        "WHERE target_external_id=? ORDER BY id DESC LIMIT 1",
        (external_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "forget"
    assert row[1] == external_id
    assert "demo" in row[2]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_rejects_unknown_id(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="does not exist"):
        await mcp.forget(tctx, external_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_rejects_bad_ulid(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="not a valid ULID"):
        await mcp.forget(tctx, external_id="nope")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_denied_without_write_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    # Read-only on style: forget needs write on the target facet's type.
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style", "project"), scope_write=())
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.forget(tctx, external_id=external_id)
    assert exc.value.required_op == "write"
    assert exc.value.required_facet_type == "style"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_already_deleted(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    await mcp.forget(tctx, external_id=external_id)
    with pytest.raises(mcp.ValidationError, match="already forgotten"):
        await mcp.forget(tctx, external_id=external_id)


# ---- cross-cutting ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capture_rejects_oversized_metadata(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError, match="serialised size"):
        await mcp.capture(
            tctx,
            content="ok",
            facet_type="style",
            metadata={"big": "x" * 5_000},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_facets_rejects_bad_since(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(open_vault, vault_path)
    with pytest.raises(mcp.ValidationError):
        await mcp.list_facets(tctx, facet_type="style", limit=5, since=-1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_show_budget_exceeded_when_snippet_alone_overflows(
    open_vault: VaultConnection,
    vault_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``show`` BudgetExceeded branch fires when per-snippet truncation
    (256 tokens by default) still leaves the response above SHOW_RESPONSE_BUDGET.

    ``monkeypatch`` drops the tool's response ceiling to 1 token so a
    single facet's snippet is guaranteed to overflow. The test pins
    that the code path raises with the stable ``budget_exceeded`` code
    instead of silently returning oversized data.
    """

    import tessera.mcp_surface.tools as tools_mod

    monkeypatch.setattr(tools_mod, "SHOW_RESPONSE_BUDGET", 1)
    tctx = await _bootstrap(open_vault, vault_path)
    external_id = open_vault.connection.execute(
        "SELECT external_id FROM facets WHERE facet_type='style' LIMIT 1"
    ).fetchone()[0]
    with pytest.raises(mcp.BudgetExceeded) as exc:
        await mcp.show(tctx, external_id=external_id)
    assert exc.value.code == "budget_exceeded"


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


# ---- v0.3 People + Skills tools -----------------------------------------


_V0_3_SCOPES: tuple[str, ...] = ("style", "project", "skill", "person")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_learn_skill_creates_and_returns_external_id(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    resp = await mcp.learn_skill(
        tctx,
        name="git-rebase",
        description="Squash branches before merge",
        procedure_md="# Procedure\n\nUse interactive rebase.",
    )
    assert resp.is_new is True
    assert resp.name == "git-rebase"
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_learn_skill_requires_write_scope_on_skill(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=("skill",),
        scope_write=("style", "project"),
    )
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.learn_skill(tctx, name="x", description="d", procedure_md="alpha")
    assert exc.value.required_facet_type == "skill"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_learn_skill_duplicate_name_surfaces_validation_error(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    await mcp.learn_skill(tctx, name="dup", description="a", procedure_md="alpha")
    with pytest.raises(mcp.ValidationError, match="already exists"):
        await mcp.learn_skill(tctx, name="dup", description="b", procedure_md="beta")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_skill_returns_full_view(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    await mcp.learn_skill(tctx, name="git-rebase", description="d", procedure_md="alpha")
    view = await mcp.get_skill(tctx, name="git-rebase")
    assert view is not None
    assert view.name == "git-rebase"
    assert view.procedure_md == "alpha"
    assert view.truncated is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_skill_returns_none_for_missing(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    assert await mcp.get_skill(tctx, name="nope") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_skill_truncates_long_body(
    open_vault: VaultConnection, vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop the budget so a small body still triggers the truncation path."""

    import tessera.mcp_surface.tools as tools_mod

    monkeypatch.setattr(tools_mod, "GET_SKILL_RESPONSE_BUDGET", 80)
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    body = "alpha bravo charlie delta echo foxtrot golf hotel " * 20
    await mcp.learn_skill(tctx, name="long", description="d", procedure_md=body)
    view = await mcp.get_skill(tctx, name="long")
    assert view is not None
    assert view.truncated is True
    assert len(view.procedure_md) < len(body)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_skills_filters_inactive_by_default(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    await mcp.learn_skill(tctx, name="active", description="d", procedure_md="a")
    await mcp.learn_skill(tctx, name="retired", description="d", procedure_md="b")
    # Flip retired off via the underlying skills module — mcp surface
    # has no metadata-edit verb at v0.3.
    from tessera.vault import skills as vault_skills

    retired = vault_skills.get_by_name(
        open_vault.connection, agent_id=tctx.verified.agent_id, name="retired"
    )
    assert retired is not None
    vault_skills.update_metadata(
        open_vault.connection, external_id=retired.external_id, active=False
    )
    listed = await mcp.list_skills(tctx)
    assert [s.name for s in listed.items] == ["active"]
    everyone = await mcp.list_skills(tctx, active_only=False)
    assert {s.name for s in everyone.items} == {"active", "retired"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_skills_requires_read_scope_on_skill(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style",), scope_write=("style",))
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.list_skills(tctx)
    assert exc.value.required_facet_type == "skill"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_person_exact_match_marked_exact(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    from tessera.vault import people as vault_people

    vault_people.insert(
        open_vault.connection,
        agent_id=tctx.verified.agent_id,
        canonical_name="Sarah Johnson",
        aliases=["Sarah", "SJ"],
    )
    resp = await mcp.resolve_person(tctx, mention="Sarah Johnson")
    assert resp.is_exact is True
    assert len(resp.matches) == 1
    assert resp.matches[0].canonical_name == "Sarah Johnson"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_person_ambiguous_returns_candidates(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    from tessera.vault import people as vault_people

    vault_people.insert(
        open_vault.connection,
        agent_id=tctx.verified.agent_id,
        canonical_name="Sarah Johnson",
    )
    vault_people.insert(
        open_vault.connection,
        agent_id=tctx.verified.agent_id,
        canonical_name="Sarah Kim",
    )
    resp = await mcp.resolve_person(tctx, mention="Sarah")
    assert resp.is_exact is False
    assert {m.canonical_name for m in resp.matches} == {"Sarah Johnson", "Sarah Kim"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_person_requires_read_scope_on_person(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("skill",), scope_write=("skill",))
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.resolve_person(tctx, mention="Sarah")
    assert exc.value.required_facet_type == "person"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_people_paginates_by_canonical_name(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    from tessera.vault import people as vault_people

    for name in ("Charlie", "Alice", "Bob"):
        vault_people.insert(
            open_vault.connection,
            agent_id=tctx.verified.agent_id,
            canonical_name=name,
        )
    resp = await mcp.list_people(tctx)
    assert [p.canonical_name for p in resp.items] == ["Alice", "Bob", "Charlie"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_people_rejects_bad_limit(open_vault: VaultConnection, vault_path: Path) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    with pytest.raises(mcp.ValidationError):
        await mcp.list_people(tctx, limit=0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_person_rejects_empty_mention(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_3_SCOPES, scope_write=_V0_3_SCOPES
    )
    with pytest.raises(mcp.ValidationError, match="must not be empty"):
        await mcp.resolve_person(tctx, mention="")


# ---- V0.5-P2 agent_profile tools ---------------------------------------


_V0_5_P2_SCOPES: tuple[str, ...] = (
    "style",
    "project",
    "skill",
    "person",
    "agent_profile",
)


def _profile_metadata() -> dict[str, object]:
    return {
        "purpose": "summarize daily standups",
        "inputs": ["standup notes"],
        "outputs": ["weekly digest"],
        "cadence": "weekly",
        "skill_refs": [],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_capture_rejects_agent_profile_facet_type(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    """ADR 0017: ``agent_profile`` writes go through
    ``register_agent_profile`` so the structured-metadata contract
    cannot be bypassed by a write-scoped caller targeting the
    generic ``capture`` tool."""

    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    with pytest.raises(mcp.ValidationError, match="register_agent_profile"):
        await mcp.capture(
            tctx,
            content="bypass attempt",
            facet_type="agent_profile",
            metadata={"any_shape": "would otherwise poison reads"},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_agent_profile_creates_facet_and_active_link(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    resp = await mcp.register_agent_profile(
        tctx, content="The digest agent", metadata=_profile_metadata()
    )
    assert resp.is_new is True
    assert resp.is_active_link is True
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_agent_profile_requires_write_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=_V0_5_P2_SCOPES,
        scope_write=("style", "project"),
    )
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.register_agent_profile(tctx, content="denied", metadata=_profile_metadata())
    assert exc.value.required_facet_type == "agent_profile"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_agent_profile_rejects_invalid_metadata(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    bad = _profile_metadata()
    del bad["purpose"]
    with pytest.raises(mcp.ValidationError, match="purpose"):
        await mcp.register_agent_profile(tctx, content="x", metadata=bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_agent_profile_without_link_leaves_pointer_unset(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    resp = await mcp.register_agent_profile(
        tctx,
        content="staged",
        metadata=_profile_metadata(),
        set_active_link=False,
    )
    assert resp.is_active_link is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_agent_profile_returns_view(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    reg = await mcp.register_agent_profile(
        tctx, content="profile body", metadata=_profile_metadata()
    )
    view = await mcp.get_agent_profile(tctx, external_id=reg.external_id)
    assert view is not None
    assert view.purpose.startswith("summarize")
    assert view.is_active_link is True
    assert view.cadence == "weekly"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_agent_profile_returns_none_for_other_agent(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    """Cross-agent reads must return None even when the ULID is leaked."""

    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    reg = await mcp.register_agent_profile(
        tctx, content="owned by agent A", metadata=_profile_metadata()
    )
    # Seed a second agent whose token has the same scope.
    open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01OTHER', 'b', 0)"
    )
    other_id = int(
        open_vault.connection.execute(
            "SELECT id FROM agents WHERE external_id='01OTHER'"
        ).fetchone()[0]
    )
    issued_other = tokens.issue(
        open_vault.connection,
        agent_id=other_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=_V0_5_P2_SCOPES, write=_V0_5_P2_SCOPES),
        now_epoch=1_000_000,
    )
    other_verified = tokens.verify_and_touch(
        open_vault.connection,
        raw_token=issued_other.raw_token,
        now_epoch=1_000_001,
    )
    other_tctx = mcp.ToolContext(
        conn=open_vault.connection,
        verified=other_verified,
        vault_path=vault_path,
        pipeline=tctx.pipeline,
        clock=lambda: 1_000_100,
    )
    assert await mcp.get_agent_profile(other_tctx, external_id=reg.external_id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_agent_profile_returns_none_for_missing_id(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    assert await mcp.get_agent_profile(tctx, external_id="01AAAAAAAAAAAAAAAAAAAAAAAA") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_agent_profiles_orders_by_capture_desc(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P2_SCOPES, scope_write=_V0_5_P2_SCOPES
    )
    first = await mcp.register_agent_profile(tctx, content="v1", metadata=_profile_metadata())
    second = await mcp.register_agent_profile(tctx, content="v2", metadata=_profile_metadata())
    resp = await mcp.list_agent_profiles(tctx)
    ordered = [item.external_id for item in resp.items]
    assert ordered.index(second.external_id) < ordered.index(first.external_id)
    assert any(item.is_active_link for item in resp.items)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_agent_profiles_requires_read_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(open_vault, vault_path, scope_read=("style", "project"), scope_write=())
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.list_agent_profiles(tctx)
    assert exc.value.required_facet_type == "agent_profile"


# ---- V0.5-P3 verification + retrospective tools -------------------------


_V0_5_P3_SCOPES: tuple[str, ...] = (
    "style",
    "project",
    "skill",
    "person",
    "agent_profile",
    "verification_checklist",
    "retrospective",
)


def _checklist_metadata(profile_external_id: str) -> dict[str, object]:
    return {
        "agent_ref": profile_external_id,
        "trigger": "pre_delivery",
        "checks": [
            {"id": "tests", "statement": "Tests cover new branches", "severity": "blocker"},
            {"id": "changelog", "statement": "Changelog entry", "severity": "warning"},
        ],
        "pass_criteria": "All blockers green",
    }


def _retrospective_metadata(profile_external_id: str, task_id: str = "task-1") -> dict[str, object]:
    return {
        "agent_ref": profile_external_id,
        "task_id": task_id,
        "went_well": ["captured the digest"],
        "gaps": ["missed migration risk"],
        "changes": [
            {"target": "verification_checklist", "change": "Add ALTER TABLE scan"},
        ],
        "outcome": "partial",
    }


async def _seed_profile(tctx: mcp.ToolContext) -> str:
    resp = await mcp.register_agent_profile(
        tctx, content="profile body", metadata=_profile_metadata()
    )
    return resp.external_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_checklist_creates_facet(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    resp = await mcp.register_checklist(
        tctx, content="checklist body", metadata=_checklist_metadata(profile_id)
    )
    assert resp.is_new is True
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_checklist_requires_write_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=_V0_5_P3_SCOPES,
        scope_write=("style", "project", "agent_profile"),
    )
    profile_id = await _seed_profile(tctx)
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.register_checklist(
            tctx, content="denied", metadata=_checklist_metadata(profile_id)
        )
    assert exc.value.required_facet_type == "verification_checklist"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_checklist_rejects_invalid_metadata(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    bad = _checklist_metadata(profile_id)
    bad["checks"] = []
    with pytest.raises(mcp.ValidationError, match="at least one"):
        await mcp.register_checklist(tctx, content="x", metadata=bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_checklist_blocks_cross_agent_ref(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    """A token cannot plant a checklist whose ``agent_ref`` points at
    another agent's profile."""

    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    # Seed agent A's profile under tctx and a separate agent B with its
    # own profile.
    profile_a = await _seed_profile(tctx)
    open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01OTHER', 'b', 0)"
    )
    other_id = int(
        open_vault.connection.execute(
            "SELECT id FROM agents WHERE external_id='01OTHER'"
        ).fetchone()[0]
    )
    issued_other = tokens.issue(
        open_vault.connection,
        agent_id=other_id,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=_V0_5_P3_SCOPES, write=_V0_5_P3_SCOPES),
        now_epoch=1_000_000,
    )
    other_verified = tokens.verify_and_touch(
        open_vault.connection,
        raw_token=issued_other.raw_token,
        now_epoch=1_000_001,
    )
    other_tctx = mcp.ToolContext(
        conn=open_vault.connection,
        verified=other_verified,
        vault_path=vault_path,
        pipeline=tctx.pipeline,
        clock=lambda: 1_000_100,
    )
    # Even though the other agent has scope, agent_ref pointing at
    # agent A's profile must be rejected.
    with pytest.raises(mcp.ValidationError, match="different agent"):
        await mcp.register_checklist(
            other_tctx,
            content="cross-agent attempt",
            metadata=_checklist_metadata(profile_a),
        )
    assert profile_a  # used


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_retrospective_creates_facet(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    resp = await mcp.record_retrospective(
        tctx,
        content="retro body",
        metadata=_retrospective_metadata(profile_id),
    )
    assert resp.is_new is True
    assert len(resp.external_id) == 26


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_retrospective_rejects_unknown_outcome(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    bad = _retrospective_metadata(profile_id)
    bad["outcome"] = "broken"
    with pytest.raises(mcp.ValidationError, match="outcome"):
        await mcp.record_retrospective(tctx, content="r", metadata=bad)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_checks_for_agent_resolves_canonical(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    checklist_resp = await mcp.register_checklist(
        tctx, content="checklist body", metadata=_checklist_metadata(profile_id)
    )
    # Re-register the profile with verification_ref set to the checklist.
    profile_meta_with_ref = dict(_profile_metadata())
    profile_meta_with_ref["verification_ref"] = checklist_resp.external_id
    revised = await mcp.register_agent_profile(
        tctx, content="profile v2", metadata=profile_meta_with_ref
    )
    view = await mcp.list_checks_for_agent(tctx, profile_external_id=revised.external_id)
    assert view is not None
    assert view.external_id == checklist_resp.external_id
    assert view.trigger == "pre_delivery"
    assert any(check.severity == "blocker" for check in view.checks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_checks_for_agent_returns_none_without_verification_ref(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault, vault_path, scope_read=_V0_5_P3_SCOPES, scope_write=_V0_5_P3_SCOPES
    )
    profile_id = await _seed_profile(tctx)
    view = await mcp.list_checks_for_agent(tctx, profile_external_id=profile_id)
    assert view is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_checks_for_agent_requires_read_scope(
    open_vault: VaultConnection, vault_path: Path
) -> None:
    tctx = await _bootstrap(
        open_vault,
        vault_path,
        scope_read=("style", "project", "agent_profile"),
        scope_write=("style", "project", "agent_profile"),
    )
    profile_id = await _seed_profile(tctx)
    with pytest.raises(mcp.ScopeDenied) as exc:
        await mcp.list_checks_for_agent(tctx, profile_external_id=profile_id)
    assert exc.value.required_facet_type == "verification_checklist"
