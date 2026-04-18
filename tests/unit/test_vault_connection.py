"""VaultConnection state validation: not-initialized, schema drift, lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.vault.connection import (
    MigrationInterruptedError,
    NeedsMigrationError,
    SchemaTooNewError,
    VaultConnection,
    VaultError,
    VaultNotInitializedError,
    _as_int,
)
from tessera.vault.encryption import derive_key

_SALT = b"\x00" * 16
_PASS = b"vaulttest"


@pytest.fixture
def fresh_vault(tmp_path: Path) -> Path:
    p = tmp_path / "vault.db"
    k = derive_key(bytearray(_PASS), _SALT)
    bootstrap(p, k)
    k.wipe()
    return p


@pytest.mark.unit
def test_open_raw_allows_missing_meta_and_defers_validation(tmp_path: Path) -> None:
    """A file-not-yet-bootstrapped is legal under open_raw for the runner."""

    empty_vault = tmp_path / "empty.db"
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with VaultConnection.open_raw(empty_vault, k) as vc:
            assert vc.connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        k.wipe()


@pytest.mark.unit
def test_open_raw_state_property_refuses_without_reload(tmp_path: Path) -> None:
    empty_vault = tmp_path / "e.db"
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with (
            VaultConnection.open_raw(empty_vault, k) as vc,
            pytest.raises(VaultError, match="raw mode"),
        ):
            _ = vc.state
    finally:
        k.wipe()


@pytest.mark.unit
def test_open_validated_rejects_unbootstrapped_vault(tmp_path: Path) -> None:
    empty_vault = tmp_path / "empty.db"
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(VaultNotInitializedError):
            VaultConnection.open(empty_vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_close_is_idempotent(fresh_vault: Path) -> None:
    k = derive_key(bytearray(_PASS), _SALT)
    vc = VaultConnection.open(fresh_vault, k)
    vc.close()
    vc.close()
    k.wipe()


@pytest.mark.unit
def test_operations_after_close_raise(fresh_vault: Path) -> None:
    k = derive_key(bytearray(_PASS), _SALT)
    vc = VaultConnection.open(fresh_vault, k)
    vc.close()
    k.wipe()
    with pytest.raises(VaultError, match="closed"):
        _ = vc.connection
    with pytest.raises(VaultError, match="closed"):
        _ = vc.state


@pytest.mark.unit
def test_open_detects_case_c_schema_too_new(
    fresh_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 0)
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(SchemaTooNewError):
            VaultConnection.open(fresh_vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_open_detects_case_a_needs_migration(
    fresh_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 2)
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(NeedsMigrationError):
            VaultConnection.open(fresh_vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_open_detects_case_d_migration_interrupted(fresh_vault: Path) -> None:
    k1 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(fresh_vault, k1) as vc:
        vc.connection.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('schema_target', '2')"
        )
    k1.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(MigrationInterruptedError):
            VaultConnection.open(fresh_vault, k2)
    finally:
        k2.wipe()


@pytest.mark.unit
def test_vault_missing_vault_id_rejected(fresh_vault: Path) -> None:
    k = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(fresh_vault, k) as vc:
        vc.connection.execute("DELETE FROM _meta WHERE key = 'vault_id'")
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(VaultNotInitializedError, match="vault_id"):
            VaultConnection.open(fresh_vault, k2)
    finally:
        k2.wipe()


@pytest.mark.unit
def test_as_int_rejects_non_integer_meta_value() -> None:
    with pytest.raises(VaultError, match="not an integer"):
        _as_int("schema_version", "banana")


@pytest.mark.unit
def test_failed_open_does_not_leak_sqlite_handles(
    fresh_vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every error raised by ``open()`` must close the underlying connection.

    Ships as a regression guard: a prior version closed the handle only when
    ``_read_state`` raised, leaking it on Case-A / Case-C / Case-D. This test
    drives Case-C and asserts the daemon can still delete the vault file on
    Windows-style exclusive-lock semantics (simulated by re-opening).
    """

    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 0)
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(SchemaTooNewError):
            VaultConnection.open(fresh_vault, k)
    finally:
        k.wipe()

    # If the earlier open leaked its connection, SQLite's WAL journal would
    # still be held; subsequent open must succeed against the clean vault.
    monkeypatch.setattr(
        "tessera.vault.connection.BINARY_SCHEMA_VERSION",
        1,
    )
    k2 = derive_key(bytearray(_PASS), _SALT)
    try:
        with VaultConnection.open(fresh_vault, k2) as vc:
            assert vc.state.schema_version == 1
    finally:
        k2.wipe()
