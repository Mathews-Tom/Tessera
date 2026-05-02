"""Capture orchestrator: insert + audit round-trip."""

from __future__ import annotations

import json

import pytest

from tessera.vault import capture, facets
from tessera.vault.connection import VaultConnection


def _make_agent(vc: VaultConnection) -> int:
    cur = vc.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01TEST', 'agent', 0)"
    )
    rowid: int = int(cur.lastrowid) if cur.lastrowid is not None else 0
    return rowid


@pytest.mark.unit
def test_capture_returns_external_id_and_writes_audit(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="project",
        content="Shipped P2 today.",
        source_tool="test",
    )
    assert result.is_duplicate is False
    assert result.external_id
    audit_rows = open_vault.connection.execute(
        "SELECT op, target_external_id, payload FROM audit_log WHERE op='facet_inserted'"
    ).fetchall()
    assert len(audit_rows) == 1
    op, target, payload_json = audit_rows[0]
    assert op == "facet_inserted"
    assert target == result.external_id
    payload = json.loads(payload_json)
    assert payload["facet_type"] == "project"
    assert payload["is_duplicate"] is False
    assert len(payload["content_hash_prefix"]) == 8


@pytest.mark.unit
def test_capture_dedup_returns_same_external_id_and_marks_duplicate(
    open_vault: VaultConnection,
) -> None:
    agent_id = _make_agent(open_vault)
    first = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="style",
        content="voice sample",
        source_tool="test",
    )
    second = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="style",
        content="voice sample",
        source_tool="test",
    )
    assert second.external_id == first.external_id
    assert second.is_duplicate is True
    rows = open_vault.connection.execute(
        "SELECT COUNT(*) FROM audit_log WHERE op='facet_inserted'"
    ).fetchone()
    # Both calls write audit rows — the second is the duplicate record the
    # forensic trail needs to explain the identical external_id.
    assert int(rows[0]) == 2


@pytest.mark.unit
def test_capture_rejects_unsupported_facet_type(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    # V0.5-P4 activated ``compiled_notebook`` for writes; ``automation``
    # is the remaining v0.5 reserved type that stays outside the
    # writable set until V0.5-P5 ships the storage-only registry.
    with pytest.raises(facets.UnsupportedFacetTypeError):
        capture.capture(
            open_vault.connection,
            agent_id=agent_id,
            facet_type="automation",
            content="not writable until v0.5-p5",
            source_tool="test",
        )


@pytest.mark.unit
def test_capture_default_embed_status_is_pending(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    result = capture.capture(
        open_vault.connection,
        agent_id=agent_id,
        facet_type="preference",
        content="Pydantic > dataclasses",
        source_tool="test",
    )
    row = open_vault.connection.execute(
        "SELECT embed_status FROM facets WHERE external_id=?",
        (result.external_id,),
    ).fetchone()
    assert row[0] == "pending"
