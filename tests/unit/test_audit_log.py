"""Audit allowlist enforcement and row shape."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tessera.vault import audit, schema


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        c.execute(stmt)
    return c


@pytest.mark.unit
def test_allowed_ops_is_non_empty() -> None:
    ops = audit.allowed_ops()
    assert "vault_init" in ops
    assert "migration_started" in ops


@pytest.mark.unit
def test_write_rejects_unknown_op(conn: sqlite3.Connection) -> None:
    with pytest.raises(audit.UnknownOpError):
        audit.write(conn, op="not_an_op", actor="system")


@pytest.mark.unit
def test_write_rejects_disallowed_payload_key(conn: sqlite3.Connection) -> None:
    with pytest.raises(audit.DisallowedPayloadKeyError) as exc:
        audit.write(
            conn,
            op="vault_init",
            actor="system",
            payload={"schema_version": 1, "facet_content": "leaked!"},
        )
    assert "facet_content" in str(exc.value)


@pytest.mark.unit
def test_write_accepts_allowlisted_payload(conn: sqlite3.Connection) -> None:
    rowid = audit.write(
        conn,
        op="vault_init",
        actor="system",
        agent_id=None,
        target_external_id=None,
        payload={"schema_version": 1, "kdf_version": 1, "vault_id": "01HVAULT"},
    )
    assert rowid > 0
    row = conn.execute("SELECT op, actor, payload FROM audit_log WHERE id = ?", (rowid,)).fetchone()
    assert row[0] == "vault_init"
    assert row[1] == "system"
    assert json.loads(row[2]) == {
        "schema_version": 1,
        "kdf_version": 1,
        "vault_id": "01HVAULT",
    }


@pytest.mark.unit
def test_write_empty_payload_is_accepted(conn: sqlite3.Connection) -> None:
    audit.write(conn, op="vault_closed", actor="cli")
    row = conn.execute("SELECT payload FROM audit_log").fetchone()
    assert json.loads(row[0]) == {}


@pytest.mark.unit
def test_allowed_keys_returns_frozen_set() -> None:
    keys = audit.allowed_keys("migration_started")
    assert "backup_path" in keys
    assert isinstance(keys, frozenset)


@pytest.mark.unit
def test_allowed_keys_rejects_unknown_op() -> None:
    with pytest.raises(audit.UnknownOpError):
        audit.allowed_keys("nonexistent")
