"""Case-D detection, step-runner idempotency, and rollback round-trip."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tessera.migration import (
    MigrationStep,
    bootstrap,
    make_backup,
    restore_backup,
    resume_interrupted,
    runner,
)
from tessera.vault.connection import (
    MigrationInterruptedError,
    VaultConnection,
)
from tessera.vault.encryption import derive_key

_FIXED_SALT = b"\x00" * 16
_PASSPHRASE = b"pass"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


@pytest.fixture
def bootstrapped(vault: Path) -> Path:
    k = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    bootstrap(vault, k)
    k.wipe()
    return vault


@pytest.mark.security
def test_case_d_vault_reject_open(bootstrapped: Path) -> None:
    """When ``_meta.schema_target`` is set, open() must refuse to serve."""

    k = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    with VaultConnection.open_raw(bootstrapped, k) as vc:
        vc.connection.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('schema_target', '2')"
        )
    k.wipe()

    k2 = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    try:
        with pytest.raises(MigrationInterruptedError):
            VaultConnection.open(bootstrapped, k2)
    finally:
        k2.wipe()


@pytest.mark.security
def test_resume_replays_registered_steps_idempotently(
    bootstrapped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic v2 migration is re-run after simulated interruption.

    Each step records its run into a counter; re-running via
    ``resume_interrupted`` must leave the counter at ``1`` because
    ``_migration_steps`` gates every step.
    """

    calls: dict[str, int] = {"add_index": 0}

    def _add_index(conn: Any) -> None:
        calls["add_index"] += 1
        conn.execute("CREATE INDEX IF NOT EXISTS test_synthetic_idx ON facets(content_hash)")

    synthetic_steps = (MigrationStep("add_index", 2, _add_index),)
    monkeypatch.setitem(runner._STEPS_BY_TARGET, 2, synthetic_steps)

    # Enter the migration through the real code path so both `schema_target`
    # and `migration_started_at` are committed the way a live run would leave
    # them — then drop the connection to simulate a crash.
    k = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    with VaultConnection.open_raw(bootstrapped, k) as vc:
        runner._enter_migration(vc.connection, target=2)
        started = vc.connection.execute(
            "SELECT value FROM _meta WHERE key = 'migration_started_at'"
        ).fetchone()
        assert started is not None
        assert started[0]
    k.wipe()

    k2 = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    state = resume_interrupted(bootstrapped, k2)
    k2.wipe()
    assert state.schema_version == 2
    assert state.schema_target is None
    assert calls["add_index"] == 1

    # A second resume is rejected: schema_target was cleared so the vault is
    # no longer in-transit.
    k3 = derive_key(bytearray(_PASSPHRASE), _FIXED_SALT)
    try:
        with pytest.raises(Exception, match="not in-transit"):
            resume_interrupted(bootstrapped, k3)
    finally:
        k3.wipe()


@pytest.mark.security
def test_backup_restore_round_trip_returns_original_bytes(bootstrapped: Path) -> None:
    original = bootstrapped.read_bytes()
    snap = make_backup(bootstrapped, target_version=2)
    bootstrapped.write_bytes(b"corrupted mid-migration")
    aborted = restore_backup(snap, bootstrapped)
    assert bootstrapped.read_bytes() == original
    assert aborted.read_bytes() == b"corrupted mid-migration"
