"""Step-runner enter/exit dance, upgrade flow, and error branches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tessera.migration import (
    MigrationError,
    MigrationStep,
    UnknownTargetError,
    bootstrap,
    runner,
    upgrade,
)
from tessera.vault.encryption import derive_key

_SALT = b"\x00" * 16
_PASS = b"test-pass"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    p = tmp_path / "vault.db"
    k = derive_key(bytearray(_PASS), _SALT)
    bootstrap(p, k)
    k.wipe()
    return p


@pytest.mark.unit
def test_upgrade_runs_synthetic_v2_migration(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _add_index(conn: Any) -> None:
        calls.append("add_index")
        conn.execute("CREATE INDEX IF NOT EXISTS syn_idx ON facets(external_id)")

    def _bump(conn: Any) -> None:
        calls.append("bump")

    synthetic = (
        MigrationStep("add_index", 2, _add_index),
        MigrationStep("bump", 2, _bump),
    )
    monkeypatch.setitem(runner._STEPS_BY_TARGET, 2, synthetic)
    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", 2)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 2)

    k = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault, k)
    k.wipe()
    assert state.schema_version == 2
    assert state.schema_target is None
    assert calls == ["add_index", "bump"]


@pytest.mark.unit
def test_upgrade_rejects_schema_newer_than_binary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", 0)

    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(MigrationError, match="newer than binary"):
            upgrade(vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_upgrade_raises_when_schema_version_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault file that was never bootstrapped cannot be upgraded."""

    empty_vault = tmp_path / "empty.db"
    k = derive_key(bytearray(_PASS), _SALT)
    # Use open_raw to PRAGMA key but never call bootstrap().
    from tessera.vault.connection import VaultConnection

    with VaultConnection.open_raw(empty_vault, k) as vc:
        vc.connection.execute("SELECT 1")
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(MigrationError, match="no schema_version"):
            upgrade(empty_vault, k2)
    finally:
        k2.wipe()


@pytest.mark.unit
def test_upgrade_unknown_target_raises(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Target 2 has no registered steps; upgrade must fail loudly."""

    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", 2)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 2)
    # No monkeypatch of _STEPS_BY_TARGET — target 2 remains unregistered.

    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(UnknownTargetError):
            upgrade(vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_resume_interrupted_rejects_clean_vault(vault: Path) -> None:
    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(MigrationError, match="not in-transit"):
            runner.resume_interrupted(vault, k)
    finally:
        k.wipe()


@pytest.mark.unit
def test_already_initialized_check_is_false_for_fresh_file(tmp_path: Path) -> None:
    """Fresh files have no `_meta` table so `_already_initialized` returns False."""

    import sqlite3

    conn = sqlite3.connect(":memory:")
    assert runner._already_initialized(conn) is False


@pytest.mark.unit
def test_step_applied_handles_missing_migration_steps_table() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    step = MigrationStep("x", 1, lambda _c: None)
    assert runner._step_applied(conn, step) is False


@pytest.mark.unit
def test_read_schema_version_returns_none_without_meta_table() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    assert runner._read_schema_version(conn) is None
    assert runner._read_schema_target(conn) is None


@pytest.mark.unit
def test_step_savepoint_rolls_back_on_failure(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step that raises must leave neither its DDL nor its marker behind."""

    def _fails(conn: Any) -> None:
        conn.execute("CREATE INDEX synthetic_fail_idx ON facets(content_hash)")
        raise RuntimeError("planned failure to exercise savepoint rollback")

    synthetic = (MigrationStep("fails", 2, _fails),)
    monkeypatch.setitem(runner._STEPS_BY_TARGET, 2, synthetic)
    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", 2)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", 2)

    k = derive_key(bytearray(_PASS), _SALT)
    try:
        with pytest.raises(RuntimeError, match="planned failure"):
            runner.upgrade(vault, k)
    finally:
        k.wipe()

    # Neither the index nor the marker survived: savepoint rollback worked.
    from tessera.vault.connection import VaultConnection

    k2 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(vault, k2) as vc:
        idx = vc.connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'synthetic_fail_idx'"
        ).fetchone()
        marker = vc.connection.execute(
            "SELECT 1 FROM _migration_steps WHERE step_name = 'fails'"
        ).fetchone()
    k2.wipe()
    assert idx is None
    assert marker is None
