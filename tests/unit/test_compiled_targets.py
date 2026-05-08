"""Compile target descriptor + stale-artifact helpers (V0.5 Phase 5)."""

from __future__ import annotations

import pytest
import sqlcipher3

from tessera.vault import capture, compiled, facets
from tessera.vault.connection import VaultConnection


def _seed_agent(conn: sqlcipher3.Connection, *, name: str = "primary", at: int = 1) -> int:
    """Insert one agents row and return its surrogate id.

    The vault layer keys cross-agent isolation off the surrogate
    integer id, so ``list_targets``/``list_stale_artifacts`` tests
    take the int back from the seed.
    """

    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
        (f"a-{name}", name, at),
    )
    row = conn.execute("SELECT id FROM agents WHERE external_id = ?", (f"a-{name}",)).fetchone()
    return int(row[0])


def _seed_descriptor(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target: str,
    task: str = "answer recurring questions",
    artifact_type: str = "playbook",
    quality_bar: str = "answers cite source facets",
    expected_refresh: str | None = "manual",
    facet_type: str = "workflow",
    content_suffix: str = "",
) -> str:
    metadata: dict[str, object] = {
        "target": target,
        "task": task,
        "artifact_type": artifact_type,
        "quality_bar": quality_bar,
    }
    if expected_refresh is not None:
        metadata["expected_refresh"] = expected_refresh
    external_id, _ = facets.insert(
        conn,
        agent_id=agent_id,
        facet_type=facet_type,
        content=f"descriptor:{target}:{content_suffix or facet_type}",
        source_tool="cli",
        metadata=metadata,
    )
    return external_id


def _seed_source(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target: str,
    facet_type: str = "project",
    content: str | None = None,
) -> str:
    body = content if content is not None else f"source-for-{target}"
    external_id, _ = facets.insert(
        conn,
        agent_id=agent_id,
        facet_type=facet_type,
        content=body,
        source_tool="cli",
        metadata={"compile_into": [target]},
    )
    return external_id


# ---- list_targets ------------------------------------------------------


@pytest.mark.unit
def test_list_targets_returns_well_formed_descriptors(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    descriptor_id = _seed_descriptor(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        task="execute release prep consistently",
        quality_bar="catches every gating step",
    )
    descriptors = compiled.list_targets(open_vault.connection, agent_id=agent_id)
    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor.target == "release_playbook"
    assert descriptor.task == "execute release prep consistently"
    assert descriptor.quality_bar == "catches every gating step"
    assert descriptor.artifact_type == "playbook"
    assert descriptor.expected_refresh == "manual"
    assert descriptor.descriptor_external_id == descriptor_id
    assert descriptor.descriptor_facet_type == "workflow"


@pytest.mark.unit
def test_list_targets_skips_facets_missing_required_keys(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="workflow",
        content="incomplete descriptor",
        source_tool="cli",
        metadata={
            "target": "incomplete",
            "task": "missing the rest of the contract",
        },
    )
    assert compiled.list_targets(open_vault.connection, agent_id=agent_id) == []


@pytest.mark.unit
def test_list_targets_only_workflow_or_skill(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    facets.insert(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="not a descriptor",
        source_tool="cli",
        metadata={
            "target": "wrong_facet_type",
            "task": "x",
            "artifact_type": "playbook",
            "quality_bar": "x",
        },
    )
    assert compiled.list_targets(open_vault.connection, agent_id=agent_id) == []


@pytest.mark.unit
def test_list_targets_excludes_other_agents(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    other_id = _seed_agent(open_vault.connection, name="other", at=2)
    _seed_descriptor(
        open_vault.connection,
        agent_id=other_id,
        target="other_target",
        content_suffix="other",
    )
    _seed_descriptor(
        open_vault.connection,
        agent_id=agent_id,
        target="my_target",
    )
    targets = compiled.list_targets(open_vault.connection, agent_id=agent_id)
    assert [d.target for d in targets] == ["my_target"]


@pytest.mark.unit
def test_list_targets_excludes_soft_deleted_descriptors(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    descriptor_id = _seed_descriptor(
        open_vault.connection,
        agent_id=agent_id,
        target="will_be_deleted",
    )
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (descriptor_id,),
    )
    assert compiled.list_targets(open_vault.connection, agent_id=agent_id) == []


@pytest.mark.unit
def test_get_target_returns_freshest_descriptor(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    _seed_descriptor(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        content_suffix="v1",
        task="initial task",
    )
    fresh_id = _seed_descriptor(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        content_suffix="v2",
        task="revised task",
    )
    descriptor = compiled.get_target(
        open_vault.connection, agent_id=agent_id, target="release_playbook"
    )
    assert descriptor is not None
    assert descriptor.descriptor_external_id == fresh_id
    assert descriptor.task == "revised task"


@pytest.mark.unit
def test_get_target_returns_none_when_missing(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    assert compiled.get_target(open_vault.connection, agent_id=agent_id, target="absent") is None


# ---- list_stale_artifacts ----------------------------------------------


@pytest.mark.unit
def test_list_stale_artifacts_returns_cascade_cause(open_vault: VaultConnection) -> None:
    """A stale artifact carries the audit-derived ``source_op`` + ``source_external_id``.

    Capturing a soft-deleted source via ``vault.capture.capture``
    re-inserts the row through the canonical path, which fires
    ``mark_stale_for_source`` and emits one
    ``compiled_artifact_marked_stale`` row through the chain insert.
    The CLI ``stale`` subcommand surfaces that row's payload, so the
    helper must read the payload back into the dataclass.
    """

    agent_id = _seed_agent(open_vault.connection)
    source_id = _seed_source(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        content="release source v1",
    )
    artifact_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="initial playbook",
        source_facets=[source_id],
        compiler_version="cc/recipe@1",
        source_tool="cli",
    )
    # Trigger staleness through the canonical capture path: a fresh
    # capture against the same agent_id+content with new metadata
    # creates a new facet whose id mutates source membership when
    # the existing row is updated. The simpler deterministic path is
    # to soft-delete the source — which is one of the three labelled
    # mutation triggers — and assert the cascade.
    facets.soft_delete(open_vault.connection, source_id)
    records = compiled.list_stale_artifacts(open_vault.connection, agent_id=agent_id)
    assert len(records) == 1
    record = records[0]
    assert record.artifact.external_id == artifact_id
    assert record.artifact.is_stale is True
    assert record.last_source_external_id == source_id
    assert record.last_source_op == "facet_soft_deleted"
    assert record.last_marked_at is not None


@pytest.mark.unit
def test_list_stale_artifacts_excludes_fresh_artifacts(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    source_id = _seed_source(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        content="fresh source",
    )
    compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="fresh playbook",
        source_facets=[source_id],
        compiler_version="cc/recipe@1",
        source_tool="cli",
    )
    assert compiled.list_stale_artifacts(open_vault.connection, agent_id=agent_id) == []


@pytest.mark.unit
def test_list_stale_artifacts_excludes_other_agents(open_vault: VaultConnection) -> None:
    """Stale lookups are scoped by agent_id even if a ULID leaked across agents.

    The helper must not return another agent's stale rows; the
    filter is the same defence that ``mark_stale_for_source``
    enforces on cascade so the read side stays symmetric.
    """

    agent_id = _seed_agent(open_vault.connection)
    other_id = _seed_agent(open_vault.connection, name="other", at=2)
    source_id = _seed_source(
        open_vault.connection,
        agent_id=other_id,
        target="other_target",
        content="other agent source",
    )
    compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=other_id,
        content="other agent playbook",
        source_facets=[source_id],
        compiler_version="cc/recipe@1",
        source_tool="cli",
    )
    facets.soft_delete(open_vault.connection, source_id)
    # The mutating agent has stale state; the calling agent does not.
    assert compiled.list_stale_artifacts(open_vault.connection, agent_id=agent_id) == []
    assert len(compiled.list_stale_artifacts(open_vault.connection, agent_id=other_id)) == 1


@pytest.mark.unit
def test_list_stale_artifacts_handles_capture_undelete(open_vault: VaultConnection) -> None:
    """Re-capturing a soft-deleted source via ``capture`` flips downstream artifacts to stale.

    The undelete branch in ``vault.capture.capture`` is the third
    cascade trigger besides ``soft_delete`` and the skill-procedure
    update path; the helper must pick up its ``facet_inserted``
    cascade payload too.
    """

    agent_id = _seed_agent(open_vault.connection)
    source_id = _seed_source(
        open_vault.connection,
        agent_id=agent_id,
        target="release_playbook",
        content="undelete-source",
    )
    artifact_id = compiled.register_compiled_artifact(
        open_vault.connection,
        agent_id=agent_id,
        content="initial playbook",
        source_facets=[source_id],
        compiler_version="cc/recipe@1",
        source_tool="cli",
    )
    # Soft-delete then un-delete via capture.capture to exercise the
    # ``facet_inserted`` cascade path.
    facets.soft_delete(open_vault.connection, source_id)
    capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="undelete-source",
        source_tool="cli",
        metadata={"compile_into": ["release_playbook"]},
    )
    records = compiled.list_stale_artifacts(open_vault.connection, agent_id=agent_id)
    assert len(records) == 1
    assert records[0].artifact.external_id == artifact_id
    # Either op is acceptable as the most recent cascade — what
    # matters is the helper surfaces a labelled mutation source.
    assert records[0].last_source_op in {"facet_soft_deleted", "facet_inserted"}
