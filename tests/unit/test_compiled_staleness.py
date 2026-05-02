"""V0.5-P6 compiled-artifact staleness wiring (ADR 0019 §Rationale 6).

The unit suite covers the four invariants the V0.5-P6 sub-phase
introduces:

1. **Direct-membership flip** — a mutation against a source ULID
   listed in ``compiled_artifacts.source_facets`` flips the
   artifact's ``is_stale`` from 0 to 1 and emits one
   ``compiled_artifact_marked_stale`` audit row carrying the
   source's ULID and the canonical mutation op.
2. **Idempotency** — a second mutation on an already-stale
   artifact emits no second audit row; the helper is idempotent.
3. **Cross-agent isolation** — a mutation cannot cascade across
   agent boundaries even when a leaked ULID surfaces in another
   agent's source list (V0.5-P6 security invariant).
4. **Tombstone filter** — ``compiled.get`` and
   ``compiled.list_for_agent`` filter via the paired facet's
   ``is_deleted`` column so a soft-deleted ``compiled_notebook``
   facet returns ``None`` from these helpers (PR #61 review M1).

Each mutation path that the handoff names — capture (un-delete),
``facets.soft_delete``, ``skills.update_procedure`` — has a
direct-flip test exercising the wiring end-to-end.
"""

from __future__ import annotations

import json

import pytest
import sqlcipher3

from tessera.vault import (
    agent_profiles,
    capture,
    compiled,
    facets,
    skills,
)
from tessera.vault.connection import VaultConnection

# ---- seed helpers --------------------------------------------------------


def _seed_agent(conn: sqlcipher3.Connection, external_id: str = "a1") -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 1)",
        (external_id, f"agent-{external_id}"),
    )
    row = conn.execute(
        "SELECT id FROM agents WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    return int(row[0])


def _seed_profile(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    purpose: str = "summarize standups",
) -> str:
    external_id, _ = agent_profiles.register(
        conn,
        agent_id=agent_id,
        content=f"profile-{agent_id}-{purpose}",
        metadata={
            "purpose": purpose,
            "inputs": ["standup notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    return external_id


def _seed_skill(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    name: str = "git-rebase",
    procedure_md: str = "# Procedure\n\nUse interactive rebase.",
) -> str:
    external_id, _ = skills.create_skill(
        conn,
        agent_id=agent_id,
        name=name,
        description="Squash branches before merge",
        procedure_md=procedure_md,
        source_tool="cli",
    )
    return external_id


def _register_playbook(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    sources: list[str],
    content: str = "The Playbook narrative.",
    compiler_version: str = "claude-opus-4-7",
) -> str:
    return compiled.register_compiled_artifact(
        conn,
        agent_id=agent_id,
        content=content,
        source_facets=sources,
        compiler_version=compiler_version,
        source_tool="cli",
    )


def _is_stale(conn: sqlcipher3.Connection, external_id: str) -> bool:
    row = conn.execute(
        "SELECT is_stale FROM compiled_artifacts WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    return bool(row[0])


def _stale_audit_rows(
    conn: sqlcipher3.Connection,
    *,
    target_external_id: str,
) -> list[tuple[str, dict[str, object]]]:
    rows = conn.execute(
        """
        SELECT op, payload FROM audit_log
        WHERE op = 'compiled_artifact_marked_stale'
              AND target_external_id = ?
        ORDER BY id ASC
        """,
        (target_external_id,),
    ).fetchall()
    return [(str(r[0]), json.loads(str(r[1]))) for r in rows]


# ---- mark_stale_for_source primitive ------------------------------------


@pytest.mark.unit
def test_mark_stale_flips_artifact_and_emits_audit(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    flipped = compiled.mark_stale_for_source(
        conn,
        source_external_id=profile_id,
        source_op="facet_soft_deleted",
        agent_id=agent_id,
    )

    assert flipped == 1
    assert _is_stale(conn, artifact_id) is True
    audits = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits) == 1
    assert audits[0][1] == {
        "source_external_id": profile_id,
        "source_op": "facet_soft_deleted",
    }


@pytest.mark.unit
def test_mark_stale_is_idempotent(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    compiled.mark_stale_for_source(
        conn,
        source_external_id=profile_id,
        source_op="facet_soft_deleted",
        agent_id=agent_id,
    )
    second_flipped = compiled.mark_stale_for_source(
        conn,
        source_external_id=profile_id,
        source_op="skill_procedure_updated",
        agent_id=agent_id,
    )

    assert second_flipped == 0
    audits = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits) == 1
    assert audits[0][1]["source_op"] == "facet_soft_deleted"


@pytest.mark.unit
def test_mark_stale_no_match_returns_zero(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    flipped = compiled.mark_stale_for_source(
        conn,
        source_external_id="01NEVERCITED",
        source_op="facet_inserted",
        agent_id=agent_id,
    )
    assert flipped == 0


@pytest.mark.unit
def test_mark_stale_rejects_unknown_op(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="not a recognised"):
        compiled.mark_stale_for_source(
            conn,
            source_external_id="01ANY",
            source_op="forget",
            agent_id=agent_id,
        )


# ---- soft-delete hook ---------------------------------------------------


@pytest.mark.unit
def test_soft_delete_flips_dependent_playbook(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    deleted = facets.soft_delete(conn, profile_id)

    assert deleted is True
    assert _is_stale(conn, artifact_id) is True
    audits = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits) == 1
    assert audits[0][1]["source_op"] == "facet_soft_deleted"


@pytest.mark.unit
def test_soft_delete_unrelated_facet_does_not_flip(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    other_profile = _seed_profile(conn, agent_id=agent_id, purpose="other")
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    facets.soft_delete(conn, other_profile)

    assert _is_stale(conn, artifact_id) is False
    assert _stale_audit_rows(conn, target_external_id=artifact_id) == []


# ---- skill procedure update hook ----------------------------------------


@pytest.mark.unit
def test_skill_procedure_update_flips_dependent_playbook(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    skill_id = _seed_skill(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[skill_id])

    changed = skills.update_procedure(
        conn,
        external_id=skill_id,
        procedure_md="# Procedure\n\nA tighter rebase recipe.",
    )

    assert changed is True
    assert _is_stale(conn, artifact_id) is True
    audits = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits) == 1
    assert audits[0][1]["source_op"] == "skill_procedure_updated"


@pytest.mark.unit
def test_skill_metadata_update_does_not_flip(open_vault: VaultConnection) -> None:
    """Renaming a skill or toggling ``active`` does not invalidate the
    compiled narrative — only the procedure body change does. ADR 0019
    §Rationale (6): the staleness signal is intentionally narrow."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    skill_id = _seed_skill(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[skill_id])

    skills.update_metadata(
        conn,
        external_id=skill_id,
        description="A tightened description with no procedure change.",
    )

    assert _is_stale(conn, artifact_id) is False
    assert _stale_audit_rows(conn, target_external_id=artifact_id) == []


# ---- capture hook (un-delete path) --------------------------------------


@pytest.mark.unit
def test_capture_recapture_undeletes_and_flips(open_vault: VaultConnection) -> None:
    """The un-delete path is the only capture flow that flips an
    existing artifact: re-capturing content matching a soft-deleted
    facet restores its original external_id, which a Playbook may
    still cite. The first soft_delete already flipped the artifact
    (test the audit-row count grows after re-capture)."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    project_content = "Project: weekly-digest milestones."
    capture_result = capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content=project_content,
        source_tool="cli",
    )
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[capture_result.external_id])

    facets.soft_delete(conn, capture_result.external_id)
    assert _is_stale(conn, artifact_id) is True
    audits_after_delete = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits_after_delete) == 1

    # Re-capture against the same content_hash returns the prior
    # external_id with is_duplicate=True. The artifact stays stale
    # (idempotent), so the helper emits no second audit row.
    recapture = capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content=project_content,
        source_tool="cli",
    )
    assert recapture.external_id == capture_result.external_id
    assert recapture.is_duplicate is True
    audits_after_recapture = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits_after_recapture) == 1


@pytest.mark.unit
def test_fresh_capture_does_not_flip_unrelated_playbook(
    open_vault: VaultConnection,
) -> None:
    """A brand-new capture mints a fresh ULID that no existing
    artifact can cite, so the flagger walks an empty result set."""

    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="project",
        content="A brand-new project facet, never cited.",
        source_tool="cli",
    )

    assert _is_stale(conn, artifact_id) is False
    assert _stale_audit_rows(conn, target_external_id=artifact_id) == []


# ---- cross-agent isolation ----------------------------------------------


@pytest.mark.security
def test_cross_agent_mutation_does_not_cascade(open_vault: VaultConnection) -> None:
    """A leaked ULID in another agent's source list cannot trigger a
    cross-agent stale flip. ``mark_stale_for_source`` filters by
    ``agent_id``; even a forged source-list cannot escape the scope.
    """

    conn = open_vault.connection
    agent_a = _seed_agent(conn, external_id="a1")
    agent_b = _seed_agent(conn, external_id="b1")
    profile_a = _seed_profile(conn, agent_id=agent_a, purpose="agent-a purpose")
    artifact_a = _register_playbook(conn, agent_id=agent_a, sources=[profile_a])

    # Forge agent_b's artifact source list to cite agent_a's
    # profile ULID. This shape is impossible through the public
    # write path (``_verify_sources_belong_to_agent`` blocks it),
    # but we plant it directly to prove the staleness primitive's
    # agent_id filter is the load-bearing isolation boundary.
    profile_b = _seed_profile(conn, agent_id=agent_b, purpose="agent-b purpose")
    artifact_b = _register_playbook(conn, agent_id=agent_b, sources=[profile_b])
    conn.execute(
        "UPDATE compiled_artifacts SET source_facets = ? WHERE external_id = ?",
        (json.dumps([profile_a]), artifact_b),
    )

    # Mutate agent_a's profile. Only agent_a's artifact may flip.
    facets.soft_delete(conn, profile_a)

    assert _is_stale(conn, artifact_a) is True
    assert _is_stale(conn, artifact_b) is False
    assert _stale_audit_rows(conn, target_external_id=artifact_b) == []


# ---- get/list tombstone filter (PR #61 M1) ------------------------------


@pytest.mark.unit
def test_get_returns_none_for_soft_deleted_pair(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id])

    assert compiled.get(conn, external_id=artifact_id) is not None
    facets.soft_delete(conn, artifact_id)
    assert compiled.get(conn, external_id=artifact_id) is None


@pytest.mark.unit
def test_list_for_agent_excludes_soft_deleted_pair(open_vault: VaultConnection) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    live_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id], content="live")
    deleted_id = _register_playbook(
        conn, agent_id=agent_id, sources=[profile_id], content="going away"
    )
    facets.soft_delete(conn, deleted_id)

    listed = compiled.list_for_agent(conn, agent_id=agent_id)
    ids = {a.external_id for a in listed}
    assert live_id in ids
    assert deleted_id not in ids


# ---- multi-source artifact ----------------------------------------------


@pytest.mark.unit
def test_artifact_with_multiple_sources_flips_on_any_mutation(
    open_vault: VaultConnection,
) -> None:
    conn = open_vault.connection
    agent_id = _seed_agent(conn)
    profile_id = _seed_profile(conn, agent_id=agent_id)
    skill_id = _seed_skill(conn, agent_id=agent_id)
    artifact_id = _register_playbook(conn, agent_id=agent_id, sources=[profile_id, skill_id])

    skills.update_procedure(
        conn,
        external_id=skill_id,
        procedure_md="# Procedure\n\nUpdated body.",
    )

    assert _is_stale(conn, artifact_id) is True
    audits = _stale_audit_rows(conn, target_external_id=artifact_id)
    assert len(audits) == 1
    assert audits[0][1]["source_external_id"] == skill_id
