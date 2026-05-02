"""ADR 0018 retrospective facet — vault layer behaviours."""

from __future__ import annotations

import pytest
import sqlcipher3

from tessera.vault import agent_profiles, retrospectives
from tessera.vault.connection import VaultConnection

_PROFILE_ULID = "01HZX1Y2Z3MNPQRSTVWXYZ0123"


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


def _valid_metadata(profile_external_id: str, task_id: str = "task-1") -> dict[str, object]:
    return {
        "agent_ref": profile_external_id,
        "task_id": task_id,
        "went_well": ["captured the digest", "no flake"],
        "gaps": ["missed migration risk"],
        "changes": [
            {"target": "verification_checklist", "change": "Add ALTER TABLE scan"},
        ],
        "outcome": "partial",
    }


# ---- metadata validation ------------------------------------------------


@pytest.mark.unit
def test_validate_metadata_accepts_minimal_shape() -> None:
    validated = retrospectives.validate_metadata(_valid_metadata(_PROFILE_ULID))
    assert validated.agent_ref == _PROFILE_ULID
    assert validated.outcome == "partial"
    assert len(validated.went_well) == 2
    assert len(validated.changes) == 1


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_outcome() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["outcome"] = "broken"
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="outcome"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_missing_agent_ref() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    del metadata["agent_ref"]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="agent_ref"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_ulid_agent_ref() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["agent_ref"] = "not-a-ulid"
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="ULID"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_top_level_key() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["author"] = "tom"
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="author"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_change_missing_field() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = [{"target": "x"}]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="missing required"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_dict_change() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = ["just a string"]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="must be an object"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_went_well_entry() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["went_well"] = ["x" * 10_000]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="exceeds max"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_dict_input() -> None:
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError):
        retrospectives.validate_metadata("nope")  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_metadata_rejects_went_well_not_a_list() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["went_well"] = "single string"
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="must be a list"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_went_well_entries() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["went_well"] = [f"item-{i}" for i in range(70)]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="entries"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_changes_not_a_list() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = "single string"
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="must be a list"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_changes() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = [{"target": "x", "change": "y"} for _ in range(70)]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="entries"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_change_unknown_field() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = [{"target": "x", "change": "y", "extra": "?"}]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="unknown keys"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_task_id() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["task_id"] = "x" * 10_000
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="exceeds max"):
        retrospectives.validate_metadata(metadata)


@pytest.mark.unit
def test_get_returns_none_for_soft_deleted(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id, _ = retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    assert retrospectives.get(open_vault.connection, external_id=external_id) is None


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_change_target(open_vault: VaultConnection) -> None:
    del open_vault
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["changes"] = [{"target": 42, "change": "y"}]
    with pytest.raises(retrospectives.InvalidRetrospectiveMetadataError, match="must be a string"):
        retrospectives.validate_metadata(metadata)


# ---- record / get / recent_for_agent contract --------------------------


@pytest.mark.unit
def test_record_inserts_facet(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id, is_new = retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    assert is_new is True
    row = open_vault.connection.execute(
        "SELECT facet_type FROM facets WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row[0] == "retrospective"


@pytest.mark.unit
def test_record_emits_facet_inserted_audit(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    rows = open_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op = 'facet_inserted'"
    ).fetchall()
    # ADR 0021 §canonical_json — payload column stores canonical bytes
    # (no whitespace) so the storage and chain-encoding paths agree.
    assert any('"facet_type":"retrospective"' in str(r[0]) for r in rows)


@pytest.mark.unit
def test_recent_for_agent_returns_only_matching_profile(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_a = _seed_profile(open_vault.connection, agent_id)
    open_vault.connection.execute(
        "UPDATE agents SET profile_facet_external_id = NULL WHERE id = ?",
        (agent_id,),
    )
    # Register a second profile with a distinct external_id.
    profile_b, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="other profile",
        metadata={
            "purpose": "different purpose",
            "inputs": ["b"],
            "outputs": ["b"],
            "cadence": "daily",
            "skill_refs": [],
        },
        source_tool="cli",
    )
    retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro for A",
        metadata=_valid_metadata(profile_a, "task-A"),
        source_tool="cli",
    )
    retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro for B",
        metadata=_valid_metadata(profile_b, "task-B"),
        source_tool="cli",
    )
    matched = retrospectives.recent_for_agent(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=profile_a,
        limit=10,
    )
    assert len(matched) == 1
    assert matched[0].metadata.task_id == "task-A"


@pytest.mark.unit
def test_recent_for_agent_orders_by_capture_desc(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    first_id, _ = retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro 1",
        metadata=_valid_metadata(profile_id, "task-1"),
        source_tool="cli",
    )
    second_id, _ = retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro 2",
        metadata=_valid_metadata(profile_id, "task-2"),
        source_tool="cli",
    )
    matched = retrospectives.recent_for_agent(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=profile_id,
        limit=10,
    )
    ids = [r.external_id for r in matched]
    assert ids.index(second_id) < ids.index(first_id)


@pytest.mark.unit
def test_recent_for_agent_respects_limit(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    for i in range(5):
        retrospectives.record(
            open_vault.connection,
            agent_id=agent_id,
            content=f"retro {i}",
            metadata=_valid_metadata(profile_id, f"task-{i}"),
            source_tool="cli",
        )
    matched = retrospectives.recent_for_agent(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=profile_id,
        limit=2,
    )
    assert len(matched) == 2


@pytest.mark.unit
def test_recent_for_agent_zero_limit_returns_empty(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    retrospectives.record(
        open_vault.connection,
        agent_id=agent_id,
        content="retro",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    matched = retrospectives.recent_for_agent(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=profile_id,
        limit=0,
    )
    assert matched == []
