"""Forward-migration state machine per docs/migration-contract.md.

The runner handles three flows:

* **Bootstrap** — a fresh vault file gets schema v1, a new ``vault_id``, and
  ``kdf_version``. No backup is needed because there is nothing to protect.
* **Upgrade** — a vault at schema ``N`` is advanced to ``M > N`` by applying
  the registered steps for targets ``N+1 .. M`` in order. Each target takes
  a pre-migration backup and flags ``_meta.schema_target`` before the first
  DDL so a crash leaves a diagnosable Case-D state.
* **Resume** — a Case-D vault replays the step sequence for its current
  ``schema_target``. Every step is idempotent and checks ``_migration_steps``
  before re-applying; rollback is the other (user-invoked) option.

For v0.1 only the bootstrap path is exercised in production because the
vault schema is at version 1. The framework is shaped so future versions
plug in by registering a new step list against their target.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import sqlcipher3
from ulid import ULID

from tessera.migration.backup import make_backup
from tessera.vault import audit
from tessera.vault.connection import (
    BINARY_SCHEMA_VERSION,
    VaultConnection,
    VaultState,
)
from tessera.vault.encryption import CURRENT_KDF_VERSION, ProtectedKey
from tessera.vault.schema import all_statements


class MigrationError(Exception):
    """Base class for migration-runner failures."""


class VaultAlreadyInitializedError(MigrationError):
    """Bootstrap attempted on a vault that already has schema rows."""


class UnknownTargetError(MigrationError):
    """No step sequence registered for the requested target version."""


StepFn = Callable[[sqlcipher3.Connection], None]


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """A single forward-migration operation.

    ``apply`` is invoked inside a savepoint together with the insert into
    ``_migration_steps`` so the pair is atomic. Nonetheless, step bodies
    **must** be idempotent: a resumed migration re-executes every step whose
    marker is absent, which is the narrow window the savepoint closes but
    does not remove (a corrupt ``_migration_steps`` table would have to be
    rebuilt from the schema). Prefer ``CREATE ... IF NOT EXISTS`` and
    ``ALTER TABLE ... ADD COLUMN`` guarded by ``pragma table_info`` checks
    over unguarded DDL.
    """

    name: str
    target_version: int
    apply: StepFn


def _install_schema(conn: sqlcipher3.Connection) -> None:
    for stmt in all_statements():
        conn.execute(stmt)


# Forward-migration step registry keyed by target version. Bootstrap (target 1
# from a fresh vault) is intentionally absent: it cannot use the step runner
# because `_meta` and `_migration_steps` do not exist until the schema DDL has
# itself been applied, so the runner's state machine has nothing to write to.
# A failed bootstrap leaves a schema-less file that the next open flags as
# VaultNotInitializedError — safe even without a checkpoint trail.
_STEPS_BY_TARGET: Final[dict[int, Sequence[MigrationStep]]] = {}


def bootstrap(path: Path, key: ProtectedKey) -> VaultState:
    """Initialize a fresh vault at ``path`` with schema v1.

    Raises :class:`VaultAlreadyInitializedError` if ``_meta.schema_version``
    already exists; callers upgrading an existing vault use :func:`upgrade`.
    """

    with VaultConnection.open_raw(path, key) as vc:
        conn = vc.connection
        if _already_initialized(conn):
            raise VaultAlreadyInitializedError(f"vault at {path} already has schema rows")
        _apply_bootstrap(conn)
        state = vc.reload_state()
    return state


def _apply_bootstrap(conn: sqlcipher3.Connection) -> None:
    vault_id = str(ULID())
    conn.execute("BEGIN")
    try:
        _install_schema(conn)
        conn.executemany(
            "INSERT INTO _meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("vault_id", vault_id),
                ("kdf_version", str(CURRENT_KDF_VERSION)),
            ],
        )
        audit.write(
            conn,
            op="vault_init",
            actor="system",
            payload={
                "schema_version": 1,
                "kdf_version": CURRENT_KDF_VERSION,
                "vault_id": vault_id,
            },
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def upgrade(path: Path, key: ProtectedKey) -> VaultState:
    """Apply every forward migration from the vault's schema to the binary's.

    Takes a pre-migration backup per target before touching DDL. On completion
    returns the observed :class:`VaultState`. Callers should call
    :func:`resume_interrupted` instead when ``_meta.schema_target`` is set.
    """

    with VaultConnection.open_raw(path, key) as vc:
        conn = vc.connection
        start = _read_schema_version(conn)
        if start is None:
            raise MigrationError(f"vault at {path} has no schema_version; call bootstrap() instead")
        if start > BINARY_SCHEMA_VERSION:
            raise MigrationError(
                f"vault schema {start} is newer than binary support ({BINARY_SCHEMA_VERSION})"
            )
        for target in range(start + 1, BINARY_SCHEMA_VERSION + 1):
            make_backup(path, target_version=target)
            _apply_target(conn, target=target)
        state = vc.reload_state()
    return state


def resume_interrupted(path: Path, key: ProtectedKey) -> VaultState:
    """Re-run the in-transit target's step sequence.

    A Case-D vault carries ``_meta.schema_target`` pointing at the target the
    previous run was advancing to. Each step checks ``_migration_steps`` and
    is a no-op if already applied; the remaining steps complete the migration
    and clear ``schema_target``.
    """

    with VaultConnection.open_raw(path, key) as vc:
        conn = vc.connection
        target = _read_schema_target(conn)
        if target is None:
            raise MigrationError(f"vault at {path} is not in-transit; nothing to resume")
        _apply_target(conn, target=target, enter=False)
        state = vc.reload_state()
    return state


def _apply_target(conn: sqlcipher3.Connection, *, target: int, enter: bool = True) -> None:
    steps = _STEPS_BY_TARGET.get(target)
    if steps is None:
        raise UnknownTargetError(f"no migration registered for target version {target}")
    if enter:
        _enter_migration(conn, target=target)
    _run_steps(conn, steps=steps, target=target)
    _exit_migration(conn, target=target)


def _run_steps(conn: sqlcipher3.Connection, *, steps: Sequence[MigrationStep], target: int) -> None:
    for step in steps:
        if _step_applied(conn, step):
            continue
        # Each step applies and marks inside a savepoint so a crash between
        # the two leaves an all-or-nothing trail. Step bodies must remain
        # idempotent as a belt-and-braces guarantee (resume re-runs until
        # the marker lands), but the savepoint removes the narrow crash
        # window that would otherwise require idempotency to be perfect.
        conn.execute("SAVEPOINT run_step")
        try:
            step.apply(conn)
            _mark_step_applied(conn, step, target)
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT run_step")
            conn.execute("RELEASE SAVEPOINT run_step")
            raise
        conn.execute("RELEASE SAVEPOINT run_step")


def _enter_migration(conn: sqlcipher3.Connection, *, target: int) -> None:
    now = _now_epoch()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('schema_target', ?)",
            (str(target),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('migration_started_at', ?)",
            (str(now),),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _exit_migration(conn: sqlcipher3.Connection, *, target: int) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("PRAGMA foreign_key_check")
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES ('schema_version', ?)",
            (str(target),),
        )
        conn.execute("DELETE FROM _meta WHERE key IN ('schema_target', 'migration_started_at')")
        conn.execute("DELETE FROM _migration_steps WHERE schema_target = ?", (target,))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _already_initialized(conn: sqlcipher3.Connection) -> bool:
    if not _meta_table_exists(conn):
        return False
    count = conn.execute("SELECT COUNT(*) FROM _meta WHERE key='schema_version'").fetchone()
    return int(count[0]) > 0


def _step_applied(conn: sqlcipher3.Connection, step: MigrationStep) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_migration_steps'"
    ).fetchone()
    if row is None:
        return False
    hit = conn.execute(
        "SELECT 1 FROM _migration_steps WHERE schema_target=? AND step_name=?",
        (step.target_version, step.name),
    ).fetchone()
    return hit is not None


def _mark_step_applied(conn: sqlcipher3.Connection, step: MigrationStep, target: int) -> None:
    conn.execute(
        "INSERT INTO _migration_steps(schema_target, step_name, applied_at) VALUES (?, ?, ?)",
        (target, step.name, _now_epoch()),
    )


def _read_schema_version(conn: sqlcipher3.Connection) -> int | None:
    return _read_meta_int(conn, "schema_version")


def _read_schema_target(conn: sqlcipher3.Connection) -> int | None:
    return _read_meta_int(conn, "schema_target")


def _read_meta_int(conn: sqlcipher3.Connection, key: str) -> int | None:
    if not _meta_table_exists(conn):
        return None
    val = conn.execute("SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
    return int(val[0]) if val else None


def _meta_table_exists(conn: sqlcipher3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
    ).fetchone()
    return row is not None


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


__all__ = [
    "MigrationError",
    "MigrationStep",
    "UnknownTargetError",
    "VaultAlreadyInitializedError",
    "bootstrap",
    "resume_interrupted",
    "upgrade",
]
