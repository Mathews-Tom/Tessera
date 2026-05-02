"""ADR 0017 agent_profile facet — vault layer behaviours.

Covers metadata validation, the register/get/list contract, and the
``agents.profile_facet_external_id`` link-mutation audit path. The
boundary tests (scope checks, MCP tool shape) live in
``test_mcp_tool_validation.py`` and ``test_mcp_tool_surface.py``.
"""

from __future__ import annotations

import pytest
import sqlcipher3

from tessera.vault import agent_profiles, audit, capture
from tessera.vault.connection import VaultConnection


def _seed_agent(conn: sqlcipher3.Connection) -> int:
    conn.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'orchestrator', 1)"
    )
    row = conn.execute("SELECT id FROM agents WHERE external_id='a1'").fetchone()
    return int(row[0])


def _valid_metadata() -> dict[str, object]:
    return {
        "purpose": "summarize daily standups into a weekly digest",
        "inputs": ["standup notes", "JIRA tickets"],
        "outputs": ["weekly digest markdown"],
        "cadence": "weekly",
        "skill_refs": [],
    }


# ---- metadata validation ------------------------------------------------


@pytest.mark.unit
def test_validate_metadata_accepts_minimal_shape() -> None:
    validated = agent_profiles.validate_metadata(_valid_metadata())
    assert validated.purpose.startswith("summarize")
    assert validated.inputs == ("standup notes", "JIRA tickets")
    assert validated.outputs == ("weekly digest markdown",)
    assert validated.cadence == "weekly"
    assert validated.skill_refs == ()
    assert validated.verification_ref is None


@pytest.mark.unit
def test_validate_metadata_rejects_missing_required_key() -> None:
    metadata = _valid_metadata()
    del metadata["purpose"]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError) as exc:
        agent_profiles.validate_metadata(metadata)
    assert "purpose" in str(exc.value)


@pytest.mark.unit
def test_validate_metadata_rejects_unknown_key() -> None:
    metadata = _valid_metadata()
    metadata["author"] = "tom"
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError) as exc:
        agent_profiles.validate_metadata(metadata)
    assert "author" in str(exc.value)


@pytest.mark.unit
def test_validate_metadata_rejects_non_dict() -> None:
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError):
        agent_profiles.validate_metadata("not a dict")  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_metadata_rejects_non_ulid_skill_ref() -> None:
    metadata = _valid_metadata()
    metadata["skill_refs"] = ["not-a-ulid"]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError) as exc:
        agent_profiles.validate_metadata(metadata)
    assert "skill_refs" in str(exc.value)


@pytest.mark.unit
def test_validate_metadata_accepts_ulid_skill_refs() -> None:
    metadata = _valid_metadata()
    metadata["skill_refs"] = ["01HZX1Y2Z3MNPQRSTVWXYZ0123"]
    validated = agent_profiles.validate_metadata(metadata)
    assert validated.skill_refs == ("01HZX1Y2Z3MNPQRSTVWXYZ0123",)


@pytest.mark.unit
def test_validate_metadata_rejects_non_ulid_verification_ref() -> None:
    metadata = _valid_metadata()
    metadata["verification_ref"] = "abc"
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_purpose() -> None:
    metadata = _valid_metadata()
    metadata["purpose"] = "x" * 10_000
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_inputs() -> None:
    metadata = _valid_metadata()
    metadata["inputs"] = [f"input-{i}" for i in range(64)]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_purpose() -> None:
    metadata = _valid_metadata()
    metadata["purpose"] = 42
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="purpose"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_empty_purpose() -> None:
    metadata = _valid_metadata()
    metadata["purpose"] = ""
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="non-empty"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_inputs_not_a_list() -> None:
    metadata = _valid_metadata()
    metadata["inputs"] = "single string instead of list"
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="must be a list"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_input_entry() -> None:
    metadata = _valid_metadata()
    metadata["inputs"] = ["ok", 42]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match=r"inputs'\]\[1\]"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_empty_input_entry() -> None:
    metadata = _valid_metadata()
    metadata["inputs"] = [""]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="non-empty"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_overlong_input_entry() -> None:
    metadata = _valid_metadata()
    metadata["inputs"] = ["x" * 10_000]
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="exceeds max"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_skill_refs_not_a_list() -> None:
    metadata = _valid_metadata()
    metadata["skill_refs"] = "01HZX1Y2Z3MNPQRSTVWXYZ0123"
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="must be a list"):
        agent_profiles.validate_metadata(metadata)


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_skill_refs() -> None:
    metadata = _valid_metadata()
    metadata["skill_refs"] = ["01HZX1Y2Z3MNPQRSTVWXYZ0123"] * 64
    with pytest.raises(agent_profiles.InvalidAgentProfileMetadataError, match="entries"):
        agent_profiles.validate_metadata(metadata)


# ---- register / get / list contract ------------------------------------


@pytest.mark.unit
def test_register_inserts_facet_and_sets_active_link(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, is_new = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="The digest agent",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    assert is_new is True
    row = open_vault.connection.execute(
        "SELECT facet_type FROM facets WHERE external_id = ?", (external_id,)
    ).fetchone()
    assert row[0] == "agent_profile"
    assert agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id) == external_id


@pytest.mark.unit
def test_register_without_active_link_leaves_pointer_null(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="staged draft",
        metadata=_valid_metadata(),
        source_tool="cli",
        set_active_link=False,
    )
    assert agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id) is None
    listed = agent_profiles.list_for_agent(open_vault.connection, agent_id=agent_id)
    assert listed[0].external_id == external_id
    assert listed[0].is_active_link is False


@pytest.mark.unit
def test_register_replacing_swaps_active_link(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    first_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v1",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    second_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v2 — added retro link",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    assert second_id != first_id
    assert agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id) == second_id
    profiles = agent_profiles.list_for_agent(open_vault.connection, agent_id=agent_id)
    by_id = {p.external_id: p for p in profiles}
    assert by_id[second_id].is_active_link is True
    assert by_id[first_id].is_active_link is False


@pytest.mark.unit
def test_get_returns_active_link_flag(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    profile = agent_profiles.get(open_vault.connection, external_id=external_id)
    assert profile is not None
    assert profile.is_active_link is True
    assert profile.metadata.purpose.startswith("summarize")


@pytest.mark.unit
def test_get_returns_none_for_soft_deleted_row(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    open_vault.connection.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = 99 WHERE external_id = ?",
        (external_id,),
    )
    assert agent_profiles.get(open_vault.connection, external_id=external_id) is None


@pytest.mark.unit
def test_clear_active_link_audits_and_returns_true(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    assert agent_profiles.clear_active_link(open_vault.connection, agent_id=agent_id) is True
    assert agent_profiles.read_active_link(open_vault.connection, agent_id=agent_id) is None
    rows = open_vault.connection.execute(
        "SELECT op FROM audit_log WHERE op = 'agent_profile_link_cleared'"
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.unit
def test_clear_active_link_noop_when_already_null(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    assert agent_profiles.clear_active_link(open_vault.connection, agent_id=agent_id) is False


@pytest.mark.unit
def test_link_set_emits_audit_with_prior_pointer(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    first_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v1",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    second_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="v2",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    payloads = open_vault.connection.execute(
        """
        SELECT target_external_id, payload FROM audit_log
        WHERE op = 'agent_profile_link_set'
        ORDER BY id ASC
        """
    ).fetchall()
    # Two link_set rows: one for the initial register, one for the swap.
    assert len(payloads) == 2
    assert payloads[0][0] == first_id
    assert payloads[1][0] == second_id


@pytest.mark.unit
def test_get_active_for_agent_returns_linked_profile(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    external_id, _ = agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="profile",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    active = agent_profiles.get_active_for_agent(open_vault.connection, agent_id=agent_id)
    assert active is not None
    assert active.external_id == external_id


@pytest.mark.unit
def test_get_active_for_agent_returns_none_when_unlinked(open_vault: VaultConnection) -> None:
    agent_id = _seed_agent(open_vault.connection)
    assert agent_profiles.get_active_for_agent(open_vault.connection, agent_id=agent_id) is None


@pytest.mark.unit
def test_audit_op_allowlist_includes_link_ops() -> None:
    ops = audit.allowed_ops()
    assert "agent_profile_link_set" in ops
    assert "agent_profile_link_cleared" in ops
    assert audit.allowed_keys("agent_profile_link_set") == frozenset({"prior_external_id"})
    assert audit.allowed_keys("agent_profile_link_cleared") == frozenset(set())


@pytest.mark.unit
def test_register_writes_facet_inserted_audit_with_agent_profile_type(
    open_vault: VaultConnection,
) -> None:
    agent_id = _seed_agent(open_vault.connection)
    agent_profiles.register(
        open_vault.connection,
        agent_id=agent_id,
        content="audited content",
        metadata=_valid_metadata(),
        source_tool="cli",
    )
    rows = open_vault.connection.execute(
        """
        SELECT payload FROM audit_log
        WHERE op = 'facet_inserted'
        """
    ).fetchall()
    # ADR 0021 §canonical_json — payload column stores canonical bytes
    # (no whitespace) so the storage and chain-encoding paths agree.
    assert any('"facet_type":"agent_profile"' in str(r[0]) for r in rows)


@pytest.mark.unit
def test_vault_capture_of_agent_profile_skips_metadata_validation(
    open_vault: VaultConnection,
) -> None:
    """The ``vault.capture.capture`` storage primitive does not parse
    agent_profile metadata — that contract lives one layer up in
    ``agent_profiles.register``. The MCP boundary blocks this path
    entirely (see ``test_mcp_capture_rejects_agent_profile_facet_type``
    in the integration suite); this test only documents that the
    storage layer itself remains agnostic."""

    agent_id = _seed_agent(open_vault.connection)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="agent_profile",
        content="raw write — direct storage call only",
        source_tool="cli",
        metadata={"any_shape": "permitted_at_storage_layer"},
    )
    assert result.external_id
