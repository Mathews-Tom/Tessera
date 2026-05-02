"""ADR 0018 verification_checklist facet — vault layer behaviours."""

from __future__ import annotations

import pytest
import sqlcipher3

from tessera.vault import agent_profiles, verification
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


def _valid_metadata(profile_external_id: str) -> dict[str, object]:
    return {
        "agent_ref": profile_external_id,
        "trigger": "pre_delivery",
        "checks": [
            {"id": "covered_tests", "statement": "Tests cover new branches", "severity": "blocker"},
            {
                "id": "changelog_updated",
                "statement": "CHANGELOG entry present",
                "severity": "warning",
            },
        ],
        "pass_criteria": "All blockers green; warnings annotated",
    }


# ---- metadata validation ------------------------------------------------


@pytest.mark.unit
def test_validate_metadata_accepts_minimal_shape() -> None:
    validated = verification.validate_metadata(_valid_metadata(_PROFILE_ULID))
    assert validated.agent_ref == _PROFILE_ULID
    assert validated.trigger == "pre_delivery"
    assert len(validated.checks) == 2
    assert validated.checks[0].severity == "blocker"


@pytest.mark.unit
def test_validate_metadata_rejects_missing_agent_ref() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    del metadata["agent_ref"]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="agent_ref"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_ulid_agent_ref() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["agent_ref"] = "not-a-ulid"
    with pytest.raises(verification.InvalidChecklistMetadataError, match="ULID"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_top_level_key() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["author"] = "tom"
    with pytest.raises(verification.InvalidChecklistMetadataError, match="author"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_empty_checks() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = []
    with pytest.raises(verification.InvalidChecklistMetadataError, match="at least one"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_severity() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [{"id": "x", "statement": "y", "severity": "critical"}]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="severity"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_duplicate_check_ids() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [
        {"id": "dup", "statement": "first", "severity": "blocker"},
        {"id": "dup", "statement": "second", "severity": "warning"},
    ]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="duplicates"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_missing_check_field() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [{"id": "x", "severity": "blocker"}]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="missing required"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_dict_input() -> None:
    with pytest.raises(verification.InvalidChecklistMetadataError):
        verification.validate_metadata("nope")  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_metadata_rejects_checks_not_a_list() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = "single string"
    with pytest.raises(verification.InvalidChecklistMetadataError, match="must be a list"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_checks() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [
        {"id": f"c{i}", "statement": "s", "severity": "informational"} for i in range(70)
    ]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="entries"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_check_unknown_field() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [
        {"id": "x", "statement": "y", "severity": "blocker", "extra": "?"},
    ]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="unknown keys"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_check_id() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["checks"] = [{"id": 42, "statement": "y", "severity": "blocker"}]
    with pytest.raises(verification.InvalidChecklistMetadataError, match="must be a string"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_pass_criteria() -> None:
    metadata = _valid_metadata(_PROFILE_ULID)
    metadata["pass_criteria"] = "x" * 10_000
    with pytest.raises(verification.InvalidChecklistMetadataError, match="exceeds max"):
        verification.validate_metadata(metadata)


@pytest.mark.unit
def test_get_canonical_for_profile_returns_none_for_missing_profile(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    resolved = verification.get_canonical_for_profile(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id="01XXXXXXXXXXXXXXXXXXXXXXXX",
    )
    assert resolved is None


# ---- register / get / list contract ------------------------------------


@pytest.mark.unit
def test_register_inserts_facet(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id, is_new = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="checklist body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    assert is_new is True
    row = open_vault.connection.execute(
        "SELECT facet_type FROM facets WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row[0] == "verification_checklist"


@pytest.mark.unit
def test_register_emits_facet_inserted_audit(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="checklist body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    rows = open_vault.connection.execute(
        "SELECT payload FROM audit_log WHERE op = 'facet_inserted'"
    ).fetchall()
    assert any('"facet_type": "verification_checklist"' in str(r[0]) for r in rows)


@pytest.mark.unit
def test_get_returns_none_for_soft_deleted(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    external_id, _ = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="checklist body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    assert verification.get(open_vault.connection, external_id=external_id) is None


@pytest.mark.unit
def test_list_for_agent_orders_by_capture_desc(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    first_metadata = _valid_metadata(profile_id)
    first_id, _ = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v1",
        metadata=first_metadata,
        source_tool="cli",
    )
    second_metadata = _valid_metadata(profile_id)
    second_metadata["pass_criteria"] = "Stricter post-rev gate"
    second_id, _ = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v2",
        metadata=second_metadata,
        source_tool="cli",
    )
    listed = verification.list_for_agent(open_vault.connection, agent_id=agent_id)
    ids = [c.external_id for c in listed]
    assert ids.index(second_id) < ids.index(first_id)


@pytest.mark.unit
def test_get_canonical_for_profile_resolves_verification_ref(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    checklist_id, _ = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="checklist body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    # Re-register the profile with verification_ref pointing at the checklist.
    agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile v2",
        metadata={
            "purpose": "summarize standups",
            "inputs": ["standup notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
            "verification_ref": checklist_id,
        },
        source_tool="cli",
    )
    new_profile_id = agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id)
    assert new_profile_id is not None
    resolved = verification.get_canonical_for_profile(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=new_profile_id,
    )
    assert resolved is not None
    assert resolved.external_id == checklist_id


@pytest.mark.unit
def test_get_canonical_for_profile_returns_none_when_no_verification_ref(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    profile_id = _seed_profile(open_vault.connection, agent_id)
    resolved = verification.get_canonical_for_profile(
        open_vault.connection,
        agent_id=agent_id,
        profile_external_id=profile_id,
    )
    assert resolved is None


@pytest.mark.unit
def test_get_canonical_for_profile_blocks_cross_agent(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('a2', 'other', 2)"
    )
    other_agent_id = int(
        open_vault.connection.execute("SELECT id FROM agents WHERE external_id='a2'").fetchone()[0]
    )
    profile_id = _seed_profile(open_vault.connection, agent_id)
    checklist_id, _ = verification.register(
        open_vault.connection,
        agent_id=agent_id,
        content="checklist body",
        metadata=_valid_metadata(profile_id),
        source_tool="cli",
    )
    agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile v2",
        metadata={
            "purpose": "summarize standups",
            "inputs": ["standup notes"],
            "outputs": ["digest"],
            "cadence": "weekly",
            "skill_refs": [],
            "verification_ref": checklist_id,
        },
        source_tool="cli",
    )
    new_profile_id = agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id)
    assert new_profile_id is not None
    # Even with the right ULID, an agent that does not own the
    # profile gets a None result rather than the checklist row.
    resolved = verification.get_canonical_for_profile(
        open_vault.connection,
        agent_id=other_agent_id,
        profile_external_id=new_profile_id,
    )
    assert resolved is None
