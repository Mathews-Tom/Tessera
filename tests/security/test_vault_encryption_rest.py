"""The vault file is unreadable without the correct passphrase."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection, VaultLockedError
from tessera.vault.encryption import derive_key, new_salt


@pytest.mark.security
def test_plain_sqlite3_cannot_read_encrypted_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    key = derive_key(bytearray(b"real-passphrase"), salt)
    bootstrap(vault, key)
    key.wipe()

    conn = sqlite3.connect(str(vault))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    conn.close()


@pytest.mark.security
def test_wrong_passphrase_is_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    k1 = derive_key(bytearray(b"correct-pass"), salt)
    bootstrap(vault, k1)
    k1.wipe()

    wrong = derive_key(bytearray(b"wrong-pass"), salt)
    try:
        with pytest.raises(VaultLockedError):
            VaultConnection.open(vault, wrong)
    finally:
        wrong.wipe()


@pytest.mark.security
def test_correct_passphrase_unlocks(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    salt = new_salt()
    k1 = derive_key(bytearray(b"phrase"), salt)
    bootstrap(vault, k1)
    k1.wipe()

    k2 = derive_key(bytearray(b"phrase"), salt)
    try:
        with VaultConnection.open(vault, k2) as vc:
            assert vc.state.schema_version == 1
    finally:
        k2.wipe()


@pytest.mark.security
def test_vault_file_has_non_sqlite_magic(tmp_path: Path) -> None:
    """Encrypted sqlcipher files do not begin with the ``SQLite format 3`` magic."""

    vault = tmp_path / "vault.db"
    salt = new_salt()
    k = derive_key(bytearray(b"pass"), salt)
    bootstrap(vault, k)
    k.wipe()
    header = vault.read_bytes()[:16]
    assert header != b"SQLite format 3\x00"
