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
from tessera.vault.schema import SCHEMA_VERSION, all_statements


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


# ---- v1 -> v2 forward migration steps -----------------------------------
#
# Schema v2 is the post-reframe (ADR 0010) vault shape: the ``facet_type``
# CHECK is the five v0.1 types plus reserved v0.3/v0.5 types; facets carry a
# ``mode`` column and a ``source_tool`` column (renamed from ``source_client``
# to match the new vocabulary); ``compiled_artifacts`` is reserved. The
# pre-reframe CHECK is table-literal, so the upgrade uses SQLite's 12-step
# table-recreate pattern: drop triggers, rename, create new, copy with a
# facet-type mapping CASE, drop old, recreate indexes/FTS/triggers, create
# the new reserved table. Old ``judgment`` rows are dropped per the plan's
# mapping table (no successor facet type in the post-reframe vocabulary).
#
# In practice no production v1 vault exists outside test harnesses — the v1
# code path only ever wrote ``episodic`` / ``semantic`` / ``style`` rows
# (see ``facets.V0_1_FACET_TYPES`` as it stood in P1). The full 7-type
# mapping is carried anyway so a dogfooding vault that did sneak in a
# ``skill`` / ``relationship`` / ``goal`` row (via raw SQL, not through the
# Python surface) migrates cleanly.


def _step_drop_v1_fts_triggers(conn: sqlcipher3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS facets_ai")
    conn.execute("DROP TRIGGER IF EXISTS facets_ad")
    conn.execute("DROP TRIGGER IF EXISTS facets_au")


def _step_rename_v1_facets(conn: sqlcipher3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_facets_v1'"
    ).fetchone()
    if row is not None:
        return
    conn.execute("ALTER TABLE facets RENAME TO _facets_v1")


def _step_create_v2_facets(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facets (
            id                     INTEGER PRIMARY KEY,
            external_id            TEXT NOT NULL UNIQUE,
            agent_id               INTEGER NOT NULL REFERENCES agents(id),
            facet_type             TEXT NOT NULL CHECK (facet_type IN
                ('identity', 'preference', 'workflow', 'project', 'style',
                 'person', 'skill', 'compiled_notebook')),
            content                TEXT NOT NULL,
            content_hash           TEXT NOT NULL,
            mode                   TEXT NOT NULL DEFAULT 'query_time'
                CHECK (mode IN ('query_time', 'write_time', 'hybrid')),
            source_tool            TEXT NOT NULL,
            captured_at            INTEGER NOT NULL,
            metadata               TEXT NOT NULL DEFAULT '{}',
            is_deleted             INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
            deleted_at             INTEGER,
            embed_model_id         INTEGER REFERENCES embedding_models(id),
            embed_status           TEXT NOT NULL DEFAULT 'pending'
                CHECK (embed_status IN ('pending', 'embedded', 'failed', 'stale')),
            embed_attempts         INTEGER NOT NULL DEFAULT 0,
            embed_last_error       TEXT,
            embed_last_attempt_at  INTEGER,
            UNIQUE(agent_id, content_hash)
        )
        """
    )


def _step_copy_v1_rows(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO facets(
            id, external_id, agent_id, facet_type, content, content_hash,
            mode, source_tool, captured_at, metadata, is_deleted, deleted_at,
            embed_model_id, embed_status, embed_attempts, embed_last_error,
            embed_last_attempt_at
        )
        SELECT
            id,
            external_id,
            agent_id,
            CASE facet_type
                WHEN 'episodic'     THEN 'project'
                WHEN 'semantic'     THEN 'preference'
                WHEN 'style'        THEN 'style'
                WHEN 'skill'        THEN 'skill'
                WHEN 'relationship' THEN 'person'
                WHEN 'goal'         THEN 'project'
            END AS facet_type,
            content,
            content_hash,
            'query_time' AS mode,
            source_client AS source_tool,
            captured_at,
            metadata,
            is_deleted,
            deleted_at,
            embed_model_id,
            embed_status,
            embed_attempts,
            embed_last_error,
            embed_last_attempt_at
        FROM _facets_v1
        WHERE facet_type != 'judgment'
        """
    )


def _step_drop_v1_facets(conn: sqlcipher3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS _facets_v1")


def _step_create_v2_indexes(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS facets_agent_type
            ON facets(agent_id, facet_type, captured_at DESC)
            WHERE is_deleted = 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS facets_captured
            ON facets(captured_at DESC) WHERE is_deleted = 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS facets_mode
            ON facets(mode, facet_type) WHERE is_deleted = 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS facets_embed_model
            ON facets(embed_model_id) WHERE is_deleted = 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS facets_embed_status
            ON facets(embed_status, embed_last_attempt_at)
            WHERE is_deleted = 0 AND embed_status IN ('pending', 'failed')
        """
    )


def _step_rebuild_fts(conn: sqlcipher3.Connection) -> None:
    # facets_fts is an external-content FTS5 table pointing at facets.id.
    # The facets table has been replaced under it, so purge any stale rows
    # and re-insert from the new live content. This keeps the same virtual
    # table (preserving tokenizer settings) rather than recreating it.
    conn.execute("DELETE FROM facets_fts")
    conn.execute(
        """
        INSERT INTO facets_fts(rowid, content)
        SELECT id, content FROM facets WHERE is_deleted = 0
        """
    )


def _step_recreate_fts_triggers(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS facets_ai AFTER INSERT ON facets BEGIN
            INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS facets_ad AFTER DELETE ON facets BEGIN
            INSERT INTO facets_fts(facets_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS facets_au AFTER UPDATE OF content ON facets BEGIN
            INSERT INTO facets_fts(facets_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
        END
        """
    )


def _step_create_compiled_artifacts(conn: sqlcipher3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compiled_artifacts (
            id                INTEGER PRIMARY KEY,
            external_id       TEXT NOT NULL UNIQUE,
            agent_id          INTEGER NOT NULL REFERENCES agents(id),
            source_facets     TEXT NOT NULL,
            artifact_type     TEXT NOT NULL,
            content           TEXT NOT NULL,
            compiled_at       INTEGER NOT NULL,
            compiler_version  TEXT NOT NULL,
            is_stale          INTEGER NOT NULL DEFAULT 0 CHECK (is_stale IN (0, 1)),
            metadata          TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS compiled_agent_type
            ON compiled_artifacts(agent_id, artifact_type, compiled_at DESC)
        """
    )


_V1_TO_V2_STEPS: Final[tuple[MigrationStep, ...]] = (
    MigrationStep("drop_v1_fts_triggers", 2, _step_drop_v1_fts_triggers),
    MigrationStep("rename_v1_facets", 2, _step_rename_v1_facets),
    MigrationStep("create_v2_facets", 2, _step_create_v2_facets),
    MigrationStep("copy_v1_rows", 2, _step_copy_v1_rows),
    MigrationStep("drop_v1_facets", 2, _step_drop_v1_facets),
    MigrationStep("create_v2_indexes", 2, _step_create_v2_indexes),
    MigrationStep("rebuild_fts", 2, _step_rebuild_fts),
    MigrationStep("recreate_fts_triggers", 2, _step_recreate_fts_triggers),
    MigrationStep("create_compiled_artifacts", 2, _step_create_compiled_artifacts),
)


# Forward-migration step registry keyed by target version. Bootstrap (target 1
# from a fresh vault) is intentionally absent: it cannot use the step runner
# because `_meta` and `_migration_steps` do not exist until the schema DDL has
# itself been applied, so the runner's state machine has nothing to write to.
# A failed bootstrap leaves a schema-less file that the next open flags as
# VaultNotInitializedError — safe even without a checkpoint trail.
_STEPS_BY_TARGET: Final[dict[int, Sequence[MigrationStep]]] = {
    2: _V1_TO_V2_STEPS,
}


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
                ("schema_version", str(SCHEMA_VERSION)),
                ("vault_id", vault_id),
                ("kdf_version", str(CURRENT_KDF_VERSION)),
            ],
        )
        audit.write(
            conn,
            op="vault_init",
            actor="system",
            payload={
                "schema_version": SCHEMA_VERSION,
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
