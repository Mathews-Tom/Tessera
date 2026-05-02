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
from tessera.vault.schema import SCHEMA_VERSION

_SALT = b"\x00" * 16
_PASS = b"test-pass"

# The tests target a synthetic "future" schema version past the real
# SCHEMA_VERSION so they can register their own step sequences without
# colliding with the registered v1 -> v2 migration.
_SYNTHETIC_TARGET = SCHEMA_VERSION + 1


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    p = tmp_path / "vault.db"
    k = derive_key(bytearray(_PASS), _SALT)
    bootstrap(p, k)
    k.wipe()
    return p


@pytest.mark.unit
def test_upgrade_runs_synthetic_migration(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _add_index(conn: Any) -> None:
        calls.append("add_index")
        conn.execute("CREATE INDEX IF NOT EXISTS syn_idx ON facets(external_id)")

    def _bump(conn: Any) -> None:
        calls.append("bump")

    synthetic = (
        MigrationStep("add_index", _SYNTHETIC_TARGET, _add_index),
        MigrationStep("bump", _SYNTHETIC_TARGET, _bump),
    )
    monkeypatch.setitem(runner._STEPS_BY_TARGET, _SYNTHETIC_TARGET, synthetic)
    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)

    k = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault, k)
    k.wipe()
    assert state.schema_version == _SYNTHETIC_TARGET
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
    """A binary-schema target past the registered ceiling must fail loudly."""

    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)
    # No monkeypatch of _STEPS_BY_TARGET — _SYNTHETIC_TARGET stays unregistered.

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


_V1_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE _meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE _migration_steps (
        schema_target  INTEGER NOT NULL,
        step_name      TEXT NOT NULL,
        applied_at     INTEGER NOT NULL,
        PRIMARY KEY (schema_target, step_name)
    )
    """,
    """
    CREATE TABLE agents (
        id           INTEGER PRIMARY KEY,
        external_id  TEXT NOT NULL UNIQUE,
        name         TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE embedding_models (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        dim        INTEGER NOT NULL,
        added_at   INTEGER NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    """
    CREATE TABLE facets (
        id                     INTEGER PRIMARY KEY,
        external_id            TEXT NOT NULL UNIQUE,
        agent_id               INTEGER NOT NULL REFERENCES agents(id),
        facet_type             TEXT NOT NULL CHECK (facet_type IN
            ('episodic', 'semantic', 'style', 'skill',
             'relationship', 'goal', 'judgment')),
        content                TEXT NOT NULL,
        content_hash           TEXT NOT NULL,
        source_client          TEXT NOT NULL,
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
    """,
    """
    CREATE VIRTUAL TABLE facets_fts USING fts5(
        content,
        content=facets,
        content_rowid=id,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER facets_ai AFTER INSERT ON facets BEGIN
        INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TABLE audit_log (
        id                  INTEGER PRIMARY KEY,
        at                  INTEGER NOT NULL,
        actor               TEXT NOT NULL,
        agent_id            INTEGER REFERENCES agents(id),
        op                  TEXT NOT NULL,
        target_external_id  TEXT,
        payload             TEXT NOT NULL DEFAULT '{}'
    )
    """,
)


def _bootstrap_v1_vault(path: Path) -> None:
    """Install the pre-reframe v1 schema directly via sqlcipher, bypassing
    the runner's bootstrap() (which now emits the current SCHEMA_VERSION DDL)."""

    from tessera.vault.connection import VaultConnection

    k = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(path, k) as vc:
        conn = vc.connection
        conn.execute("BEGIN")
        for stmt in _V1_SCHEMA_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?), (?, ?), (?, ?)",
            ("schema_version", "1", "vault_id", "01TESTVAULT", "kdf_version", "1"),
        )
        conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'a', 0)")
        conn.execute("COMMIT")
    k.wipe()


# v2 was the post-reframe ADR-0010 schema before v0.3 unlocked People +
# Skills. The DDL here is frozen at that shape so the v2 -> v3 migration
# tests have a real prior-version vault to upgrade rather than relying
# on the live ``schema.all_statements()`` (which now emits v3).
_V2_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE _meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE _migration_steps (
        schema_target  INTEGER NOT NULL,
        step_name      TEXT NOT NULL,
        applied_at     INTEGER NOT NULL,
        PRIMARY KEY (schema_target, step_name)
    )
    """,
    """
    CREATE TABLE agents (
        id           INTEGER PRIMARY KEY,
        external_id  TEXT NOT NULL UNIQUE,
        name         TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE embedding_models (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        dim        INTEGER NOT NULL,
        added_at   INTEGER NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    """
    CREATE TABLE facets (
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
    """,
    """
    CREATE VIRTUAL TABLE facets_fts USING fts5(
        content,
        content=facets,
        content_rowid=id,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER facets_ai AFTER INSERT ON facets BEGIN
        INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TABLE compiled_artifacts (
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
    """,
    """
    CREATE TABLE capabilities (
        id                   INTEGER PRIMARY KEY,
        agent_id             INTEGER NOT NULL REFERENCES agents(id),
        client_name          TEXT NOT NULL,
        token_hash           TEXT NOT NULL UNIQUE,
        salt                 TEXT NOT NULL,
        scopes               TEXT NOT NULL,
        token_class          TEXT NOT NULL CHECK (token_class IN ('session', 'service', 'subagent')),
        created_at           INTEGER NOT NULL,
        expires_at           INTEGER NOT NULL,
        last_used_at         INTEGER,
        revoked_at           INTEGER,
        refresh_token_hash   TEXT UNIQUE,
        refresh_salt         TEXT,
        refresh_expires_at   INTEGER
    )
    """,
    """
    CREATE TABLE audit_log (
        id                  INTEGER PRIMARY KEY,
        at                  INTEGER NOT NULL,
        actor               TEXT NOT NULL,
        agent_id            INTEGER REFERENCES agents(id),
        op                  TEXT NOT NULL,
        target_external_id  TEXT,
        payload             TEXT NOT NULL DEFAULT '{}'
    )
    """,
)


def _bootstrap_v2_vault(path: Path) -> None:
    """Install the v2 (post-reframe, pre-v0.3) schema directly."""

    from tessera.vault.connection import VaultConnection

    k = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(path, k) as vc:
        conn = vc.connection
        conn.execute("BEGIN")
        for stmt in _V2_SCHEMA_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?), (?, ?), (?, ?)",
            ("schema_version", "2", "vault_id", "01TESTVAULT2", "kdf_version", "1"),
        )
        conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'a', 0)")
        conn.execute("COMMIT")
    k.wipe()


@pytest.mark.unit
def test_v1_to_v2_migration_maps_retired_facet_types(tmp_path: Path) -> None:
    """The real v1 -> v2 step list remaps retired facet types per ADR 0010."""

    vault_path = tmp_path / "v1.db"
    _bootstrap_v1_vault(vault_path)

    # Seed one row of each pre-reframe facet type that still maps onto v2.
    k = derive_key(bytearray(_PASS), _SALT)
    from tessera.vault.connection import VaultConnection

    v1_rows = [
        ("e1", "episodic", "dated event"),
        ("e2", "semantic", "stable fact"),
        ("e3", "style", "voice sample"),
        ("e4", "skill", "procedure"),
        ("e5", "relationship", "colleague kim"),
        ("e6", "goal", "ship v0.1"),
        ("e7", "judgment", "trade-off call"),
    ]
    with VaultConnection.open_raw(vault_path, k) as vc:
        conn = vc.connection
        for eid, ftype, content in v1_rows:
            conn.execute(
                """
                INSERT INTO facets(external_id, agent_id, facet_type, content,
                                    content_hash, source_client, captured_at)
                VALUES (?, 1, ?, ?, ?, ?, ?)
                """,
                (eid, ftype, content, f"h-{eid}", "cli", 1_000),
            )
    k.wipe()

    # Run the real upgrade. The runner advances all the way to the
    # binary's BINARY_SCHEMA_VERSION — both the v1 -> v2 remapping
    # and every additive surface up to the current version land in
    # one call.
    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION

    # Verify the remapping: episodic -> project, semantic -> preference,
    # style -> style, skill -> skill, relationship -> person,
    # goal -> project, judgment dropped.
    k3 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open(vault_path, k3) as vc:
        conn = vc.connection
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT external_id, facet_type FROM facets ORDER BY external_id"
            ).fetchall()
        }
        # Every row carries the new ``mode`` and ``source_tool`` columns.
        modes = {
            row[0]: row[1]
            for row in conn.execute("SELECT external_id, mode FROM facets").fetchall()
        }
        source_tools = {
            row[0]: row[1]
            for row in conn.execute("SELECT external_id, source_tool FROM facets").fetchall()
        }
        # The reserved ``compiled_artifacts`` table now exists and is empty.
        count = conn.execute("SELECT COUNT(*) FROM compiled_artifacts").fetchone()[0]
        # v3-added structure is in place.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(facets)").fetchall()}
        people_count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        mentions_count = conn.execute("SELECT COUNT(*) FROM person_mentions").fetchone()[0]
    k3.wipe()

    assert rows == {
        "e1": "project",
        "e2": "preference",
        "e3": "style",
        "e4": "skill",
        "e5": "person",
        "e6": "project",
    }
    # e7 (judgment) was dropped.
    assert "e7" not in rows
    assert set(modes.values()) == {"query_time"}
    assert set(source_tools.values()) == {"cli"}
    assert count == 0
    assert "disk_path" in cols
    assert people_count == 0
    assert mentions_count == 0


@pytest.mark.unit
def test_v2_to_v3_migration_adds_disk_path_and_people_tables(tmp_path: Path) -> None:
    """A vault bootstrapped at v2 must upgrade to v3 without losing rows."""

    # Install the v2 schema directly. v2 is the post-reframe shape: the
    # v3 additive surface (disk_path column, people, person_mentions) is
    # absent. We then call upgrade() and verify the additions land.
    vault_path = tmp_path / "v2.db"
    _bootstrap_v2_vault(vault_path)

    # Seed a couple of facets at v2 so we can prove row preservation.
    k = derive_key(bytearray(_PASS), _SALT)
    from tessera.vault.connection import VaultConnection

    with VaultConnection.open_raw(vault_path, k) as vc:
        conn = vc.connection
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES ('seed1', 1, 'project', 'work-in-progress', 'h-seed1', 'cli', 1000)
            """
        )
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES ('seed2', 1, 'preference', 'no emojis', 'h-seed2', 'cli', 1001)
            """
        )
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION

    k3 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open(vault_path, k3) as vc:
        conn = vc.connection
        cols = {row[1] for row in conn.execute("PRAGMA table_info(facets)").fetchall()}
        assert "disk_path" in cols

        # Both v2-era rows survive with NULL disk_path.
        seeded = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT external_id, disk_path FROM facets ORDER BY external_id"
            ).fetchall()
        }
        assert seeded == {"seed1": None, "seed2": None}

        # New tables exist and are empty.
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM person_mentions").fetchone()[0] == 0

        # The partial-unique index lives on the new column.
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='facets_disk_path'"
        ).fetchone()
        assert idx is not None
    k3.wipe()


@pytest.mark.unit
def test_v2_to_v3_migration_is_idempotent_under_resume(tmp_path: Path) -> None:
    """Running upgrade twice must leave the vault at the current schema with no errors."""

    vault_path = tmp_path / "v2-resume.db"
    _bootstrap_v2_vault(vault_path)

    k = derive_key(bytearray(_PASS), _SALT)
    upgrade(vault_path, k)
    k.wipe()

    # A second call is a no-op: schema_version is already at the binary
    # ceiling so the for-loop has zero iterations.
    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION


_V3_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE _meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE _migration_steps (
        schema_target  INTEGER NOT NULL,
        step_name      TEXT NOT NULL,
        applied_at     INTEGER NOT NULL,
        PRIMARY KEY (schema_target, step_name)
    )
    """,
    """
    CREATE TABLE agents (
        id           INTEGER PRIMARY KEY,
        external_id  TEXT NOT NULL UNIQUE,
        name         TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE embedding_models (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        dim        INTEGER NOT NULL,
        added_at   INTEGER NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    """
    CREATE TABLE facets (
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
        disk_path              TEXT,
        UNIQUE(agent_id, content_hash)
    )
    """,
    """
    CREATE VIRTUAL TABLE facets_fts USING fts5(
        content,
        content=facets,
        content_rowid=id,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER facets_ai AFTER INSERT ON facets BEGIN
        INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TABLE compiled_artifacts (
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
    """,
    """
    CREATE TABLE people (
        id              INTEGER PRIMARY KEY,
        external_id     TEXT NOT NULL UNIQUE,
        agent_id        INTEGER NOT NULL REFERENCES agents(id),
        canonical_name  TEXT NOT NULL,
        aliases         TEXT NOT NULL DEFAULT '[]',
        metadata        TEXT NOT NULL DEFAULT '{}',
        created_at      INTEGER NOT NULL,
        UNIQUE(agent_id, canonical_name)
    )
    """,
    """
    CREATE TABLE person_mentions (
        id          INTEGER PRIMARY KEY,
        facet_id    INTEGER NOT NULL REFERENCES facets(id) ON DELETE CASCADE,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        confidence  REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
        UNIQUE(facet_id, person_id)
    )
    """,
    """
    CREATE TABLE capabilities (
        id                   INTEGER PRIMARY KEY,
        agent_id             INTEGER NOT NULL REFERENCES agents(id),
        client_name          TEXT NOT NULL,
        token_hash           TEXT NOT NULL UNIQUE,
        salt                 TEXT NOT NULL,
        scopes               TEXT NOT NULL,
        token_class          TEXT NOT NULL CHECK (token_class IN ('session', 'service', 'subagent')),
        created_at           INTEGER NOT NULL,
        expires_at           INTEGER NOT NULL,
        last_used_at         INTEGER,
        revoked_at           INTEGER,
        refresh_token_hash   TEXT UNIQUE,
        refresh_salt         TEXT,
        refresh_expires_at   INTEGER
    )
    """,
    """
    CREATE TABLE audit_log (
        id                  INTEGER PRIMARY KEY,
        at                  INTEGER NOT NULL,
        actor               TEXT NOT NULL,
        agent_id            INTEGER REFERENCES agents(id),
        op                  TEXT NOT NULL,
        target_external_id  TEXT,
        payload             TEXT NOT NULL DEFAULT '{}'
    )
    """,
)


def _bootstrap_v3_vault(path: Path) -> None:
    """Install the v3 (post-v0.3, pre-V0.5-P1) schema directly.

    Used by the v3 -> v4 cumulative migration tests so the runner is
    exercised against a real prior-version vault rather than the live
    ``schema.all_statements()`` (which now emits v4).
    """

    from tessera.vault.connection import VaultConnection

    k = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open_raw(path, k) as vc:
        conn = vc.connection
        conn.execute("BEGIN")
        for stmt in _V3_SCHEMA_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES (?, ?), (?, ?), (?, ?)",
            ("schema_version", "3", "vault_id", "01TESTVAULT3", "kdf_version", "1"),
        )
        conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'a', 0)")
        conn.execute("COMMIT")
    k.wipe()


@pytest.mark.unit
def test_v3_to_v4_migration_extends_check_and_adds_agents_link(tmp_path: Path) -> None:
    """v3 → v4 must add volatility, the v0.5 facet-type reservations,
    and the ``agents.profile_facet_external_id`` linkage column."""

    vault_path = tmp_path / "v3.db"
    _bootstrap_v3_vault(vault_path)

    k = derive_key(bytearray(_PASS), _SALT)
    from tessera.vault.connection import VaultConnection

    with VaultConnection.open_raw(vault_path, k) as vc:
        vc.connection.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES ('seed-pre', 1, 'project', 'preserved across migration',
                    'h-seed-pre', 'cli', 1000)
            """
        )
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION

    k3 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open(vault_path, k3) as vc:
        conn = vc.connection
        # Pre-existing row survived the table-recreate.
        row = conn.execute(
            "SELECT facet_type, volatility, ttl_seconds FROM facets WHERE external_id='seed-pre'"
        ).fetchone()
        assert row[0] == "project"
        assert row[1] == "persistent"
        assert row[2] is None

        # New facet types reserved by V0.5-P2 are CHECK-permitted.
        for ftype in (
            "agent_profile",
            "verification_checklist",
            "retrospective",
            "automation",
        ):
            conn.execute(
                """
                INSERT INTO facets(external_id, agent_id, facet_type, content,
                                   content_hash, source_tool, captured_at)
                VALUES (?, 1, ?, 'x', ?, 'cli', 2000)
                """,
                (f"f-{ftype}", ftype, f"h-{ftype}"),
            )

        # FK linkage column is present and nullable on agents.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
        assert "profile_facet_external_id" in cols
        link = conn.execute("SELECT profile_facet_external_id FROM agents WHERE id = 1").fetchone()
        assert link[0] is None
    k3.wipe()


@pytest.mark.unit
def test_v3_to_v4_migration_backfills_audit_chain(tmp_path: Path) -> None:
    """v3 → v4 must populate ``prev_hash`` / ``row_hash`` for pre-upgrade rows."""

    vault_path = tmp_path / "v3-audit.db"
    _bootstrap_v3_vault(vault_path)

    k = derive_key(bytearray(_PASS), _SALT)
    from tessera.vault.connection import VaultConnection

    with VaultConnection.open_raw(vault_path, k) as vc:
        # Seed three pre-upgrade audit rows in id-ASC order.
        for index in range(3):
            vc.connection.execute(
                """
                INSERT INTO audit_log(at, actor, agent_id, op, target_external_id, payload)
                VALUES (?, 'cli', 1, 'vault_opened', NULL, ?)
                """,
                (1000 + index, '{"schema_version": 3}'),
            )
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION

    k3 = derive_key(bytearray(_PASS), _SALT)
    with VaultConnection.open(vault_path, k3) as vc:
        from tessera.vault.audit_chain import verify_chain

        rows = vc.connection.execute(
            "SELECT id, prev_hash, row_hash FROM audit_log ORDER BY id ASC"
        ).fetchall()
        # Pre-upgrade rows now carry chained hashes.
        for index, row in enumerate(rows):
            assert row[2] != ""
            if index == 0:
                assert row[1] == ""
            else:
                assert row[1] == rows[index - 1][2]
        # Walking the populated chain end-to-end succeeds.
        outcome = verify_chain(vc.connection)
        assert outcome.total_rows == len(rows)
        assert outcome.head is not None
    k3.wipe()


@pytest.mark.unit
def test_v3_to_v4_migration_is_idempotent_under_resume(tmp_path: Path) -> None:
    """Re-running upgrade on a fully migrated v4 vault is a no-op."""

    vault_path = tmp_path / "v3-resume.db"
    _bootstrap_v3_vault(vault_path)

    k = derive_key(bytearray(_PASS), _SALT)
    upgrade(vault_path, k)
    k.wipe()

    k2 = derive_key(bytearray(_PASS), _SALT)
    state = upgrade(vault_path, k2)
    k2.wipe()
    assert state.schema_version == SCHEMA_VERSION


@pytest.mark.unit
def test_step_savepoint_rolls_back_on_failure(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step that raises must leave neither its DDL nor its marker behind."""

    def _fails(conn: Any) -> None:
        conn.execute("CREATE INDEX synthetic_fail_idx ON facets(content_hash)")
        raise RuntimeError("planned failure to exercise savepoint rollback")

    synthetic = (MigrationStep("fails", _SYNTHETIC_TARGET, _fails),)
    monkeypatch.setitem(runner._STEPS_BY_TARGET, _SYNTHETIC_TARGET, synthetic)
    monkeypatch.setattr(runner, "BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)
    monkeypatch.setattr("tessera.vault.connection.BINARY_SCHEMA_VERSION", _SYNTHETIC_TARGET)

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
