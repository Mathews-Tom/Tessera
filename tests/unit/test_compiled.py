"""ADR 0019 compiled_notebook (AgenticOS Playbook) — vault layer."""

from __future__ import annotations

import pytest
import sqlcipher3

from tessera.vault import agent_profiles, compiled, facets
from tessera.vault.connection import VaultConnection


def _seed_agent(conn: sqlcipher3.Connection) -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'orchestrator', 1)"
    )
    row = conn.execute("SELECT id FROM agents WHERE external_id='a1'").fetchone()
    return int(row[0])


def _seed_profile(conn: sqlcipher3.Connection, agent_id: int) -> str:
    external_id, _ = agent_profiles.register(
        conn,
        agent_id=agent_id,
        content="profile",
        metadata={
            "purpose": "summarize standups",
            "inputs": ["standup notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    return external_id


def _seed_profile_for(conn: sqlcipher3.Connection, agent_id: int) -> str:
    """Seed a profile for an explicit agent_id with content keyed off id.

    Reused by the cross-agent guard test to mint a second profile
    under a different agent without colliding on the
    UNIQUE(agent_id, content_hash) constraint that ``_seed_profile``
    uses for the primary seeded agent.
    """

    external_id, _ = agent_profiles.register(
        conn,
        agent_id=agent_id,
        content=f"other-agent profile {agent_id}",
        metadata={
            "purpose": "different purpose",
            "inputs": ["other"],
            "outputs": ["other"],
            "cadence": "daily",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    return external_id


# ---- register_compiled_artifact contract -------------------------------


@pytest.mark.unit
def test_register_writes_pair_and_audit(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="The Playbook for the digest agent.",
        source_facets=[profile_id],
        compiler_version="claude-opus-4-7",
        source_tool="cli",
    )
    facet_row = open_vault.connection.execute(
        "SELECT facet_type, mode FROM facets WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    assert facet_row[0] == "compiled_notebook"
    assert facet_row[1] == "write_time"
    artifact_row = open_vault.connection.execute(
        "SELECT artifact_type, compiler_version, is_stale FROM compiled_artifacts WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    assert artifact_row[0] == "playbook"
    assert artifact_row[1] == "claude-opus-4-7"
    assert artifact_row[2] == 0
    audit_rows = open_vault.connection.execute(
        "SELECT op, target_external_id FROM audit_log WHERE op = 'compiled_artifact_registered'"
    ).fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0][1] == external_id


@pytest.mark.unit
def test_register_pair_share_external_id(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="body",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
    )
    facet = facets.get(open_vault.connection, external_id)
    artifact = compiled.get(open_vault.connection, external_id=external_id)
    assert facet is not None
    assert artifact is not None
    assert facet.external_id == artifact.external_id


@pytest.mark.unit
def test_register_rejects_empty_source_facets(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="at least one"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="x",
            source_facets=[],
            compiler_version="v1",
            source_tool="cli",
        )


@pytest.mark.unit
def test_register_rejects_overlong_compiler_version(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="exceeds max"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="x",
            source_facets=[profile_id],
            compiler_version="x" * 200,
            source_tool="cli",
        )


@pytest.mark.unit
def test_register_rejects_non_string_source(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="non-empty string"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="x",
            source_facets=[42],  # type: ignore[list-item]
            compiler_version="v1",
            source_tool="cli",
        )


@pytest.mark.unit
def test_register_rejects_nonexistent_source_ulid(open_vault: VaultConnection) -> None:
    """Source ULIDs must reference live facets owned by the calling
    agent — provenance integrity is part of the audit posture."""

    agent_id = _seed_agent(open_vault.connection)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="missing"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="x",
            source_facets=["01ZZZZZZZZZZZZZZZZZZZZZZZZ"],
            compiler_version="v1",
            source_tool="cli",
        )


@pytest.mark.unit
def test_register_rejects_cross_agent_source_ulid(open_vault: VaultConnection) -> None:
    """A write-scoped caller must not be able to plant a Playbook
    claiming sources owned by another agent — the per-agent guard
    on ``source_facets`` is the symmetric write-side check that
    matches the read-side cross-agent isolation in ``get`` /
    ``list_compile_sources``."""

    agent_id = _seed_agent(open_vault.connection)
    open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('a2', 'other', 2)"
    )
    other_agent_id = int(
        open_vault.connection.execute("SELECT id FROM agents WHERE external_id='a2'").fetchone()[0]
    )
    other_profile_id = _seed_profile_for(open_vault.connection, other_agent_id)
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="missing"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="cross-agent attempt",
            source_facets=[other_profile_id],
            compiler_version="v1",
            source_tool="cli",
        )


@pytest.mark.unit
def test_register_rejects_soft_deleted_source(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (profile_id,),
    )
    with pytest.raises(compiled.InvalidCompiledArtifactError, match="missing"):
        compiled.register_compiled_artifact(
            open_vault.connection,
            agent_id=agent_id,
            content="x",
            source_facets=[profile_id],
            compiler_version="v1",
            source_tool="cli",
        )


# ---- get / list contract -----------------------------------------------


@pytest.mark.unit
def test_get_returns_full_artifact(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="full body",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
    )
    artifact = compiled.get(open_vault.connection, external_id=external_id)
    assert artifact is not None
    assert artifact.content == "full body"
    assert artifact.source_facets == (profile_id,)
    assert artifact.is_stale is False


@pytest.mark.unit
def test_get_returns_none_for_missing(open_vault: VaultConnection) -> None:
    assert compiled.get(open_vault.connection, external_id="01XXXXXXXXXXXXXXXXXXXXXXXX") is None


@pytest.mark.unit
def test_list_for_agent_orders_by_compiled_at_desc(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    first = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="v1",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
    )
    second = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="v2",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
    )
    listed = compiled.list_for_agent(open_vault.connection, agent_id=agent_id)
    ids = [a.external_id for a in listed]
    assert ids.index(second) < ids.index(first)


@pytest.mark.unit
def test_list_for_agent_filters_by_artifact_type(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    playbook_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="playbook body",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
        artifact_type="playbook",
    )
    research_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="research body",
        source_facets=[profile_id],
        compiler_version="v1",
        source_tool="cli",
        artifact_type="research",
    )
    listed = compiled.list_for_agent(
        open_vault.connection, agent_id=agent_id, artifact_type="playbook"
    )
    ids = [a.external_id for a in listed]
    assert playbook_id in ids
    assert research_id not in ids


# ---- list_for_compilation contract -------------------------------------


@pytest.mark.unit
def test_list_for_compilation_returns_tagged_sources(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="tagged project",
        source_tool="cli",
        metadata={"compile_into": ["playbook_main"]},
    )
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="untagged project",
        source_tool="cli",
        metadata={},
    )
    sources = compiled.list_for_compilation(
        open_vault.connection, agent_id=agent_id, target="playbook_main"
    )
    assert len(sources) == 1
    assert sources[0].content == "tagged project"
    assert sources[0].facet_type == "project"


@pytest.mark.unit
def test_list_for_compilation_excludes_unsupported_types(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    # ``identity`` is not a primary input per ADR 0019; even with a
    # compile_into tag it must not appear in the source list.
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="identity",
        content="role: senior researcher",
        source_tool="cli",
        metadata={"compile_into": ["playbook_main"]},
    )
    sources = compiled.list_for_compilation(
        open_vault.connection, agent_id=agent_id, target="playbook_main"
    )
    assert sources == []


@pytest.mark.unit
def test_list_for_compilation_filters_by_target(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="for playbook A",
        source_tool="cli",
        metadata={"compile_into": ["playbook_a"]},
    )
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="for playbook B",
        source_tool="cli",
        metadata={"compile_into": ["playbook_b"]},
    )
    sources_a = compiled.list_for_compilation(
        open_vault.connection, agent_id=agent_id, target="playbook_a"
    )
    sources_b = compiled.list_for_compilation(
        open_vault.connection, agent_id=agent_id, target="playbook_b"
    )
    assert {s.content for s in sources_a} == {"for playbook A"}
    assert {s.content for s in sources_b} == {"for playbook B"}


@pytest.mark.unit
def test_list_for_compilation_excludes_soft_deleted(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, _ = facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="will be deleted",
        source_tool="cli",
        metadata={"compile_into": ["playbook_main"]},
    )
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    sources = compiled.list_for_compilation(
        open_vault.connection, agent_id=agent_id, target="playbook_main"
    )
    assert sources == []
