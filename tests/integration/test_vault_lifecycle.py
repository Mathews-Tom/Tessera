"""End-to-end encrypted vault lifecycle: bootstrap, open, mutate, reopen."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.migration import bootstrap, upgrade
from tessera.vault import facets
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt
from tessera.vault.schema import SCHEMA_VERSION


@pytest.mark.integration
def test_bootstrap_creates_schema_at_current_version(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    key = derive_key(bytearray(b"passphrase"), salt)
    state = bootstrap(vault, key)
    key.wipe()
    assert state.schema_version == SCHEMA_VERSION
    assert state.schema_target is None
    assert state.vault_id
    assert state.kdf_version == 1


@pytest.mark.integration
def test_vault_round_trip_writes_and_reads_facet(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()

    k1 = derive_key(bytearray(b"passphrase"), salt)
    bootstrap(vault, k1)
    k1.wipe()

    k2 = derive_key(bytearray(b"passphrase"), salt)
    with VaultConnection.open(vault, k2) as vc:
        conn = vc.connection
        conn.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
            ("01AGENT", "writer", 1),
        )
        agent_id = int(conn.execute("SELECT id FROM agents").fetchone()[0])
        eid, is_new = facets.insert(
            conn,
            agent_id=agent_id,
            facet_type="style",
            content="short, punchy sentences are my voice",
            source_tool="test",
        )
        assert is_new is True
    k2.wipe()

    k3 = derive_key(bytearray(b"passphrase"), salt)
    with VaultConnection.open(vault, k3) as vc:
        f = facets.get(vc.connection, eid)
    k3.wipe()
    assert f is not None
    assert f.content == "short, punchy sentences are my voice"


@pytest.mark.integration
def test_upgrade_is_noop_when_already_at_binary_version(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    k1 = derive_key(bytearray(b"pass"), salt)
    bootstrap(vault, k1)
    k1.wipe()

    k2 = derive_key(bytearray(b"pass"), salt)
    state = upgrade(vault, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION


@pytest.mark.integration
def test_bootstrap_writes_vault_init_audit_row(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    k1 = derive_key(bytearray(b"pass"), salt)
    state = bootstrap(vault, k1)
    k1.wipe()

    k2 = derive_key(bytearray(b"pass"), salt)
    with VaultConnection.open(vault, k2) as vc:
        row = vc.connection.execute(
            "SELECT op, actor, payload FROM audit_log ORDER BY id"
        ).fetchone()
    k2.wipe()
    assert row[0] == "vault_init"
    assert row[1] == "system"
    assert state.vault_id in row[2]
