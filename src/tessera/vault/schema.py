"""v2 vault schema per docs/system-design.md §Vault schema and §Failure taxonomy.

Schema v2 is the first post-reframe schema: the ``facet_type`` CHECK
reflects the five-facet v0.1 vocabulary plus reserved v0.3/v0.5 types
(ADR 0010), each facet row carries a ``mode`` column and a
``source_tool`` column, and the ``compiled_artifacts`` table is
reserved (empty but present) for v0.5 write-time compilation.

The schema is emitted as a list of ordered DDL statements. The
migration runner applies them inside a single transaction guarded by
``_meta.schema_target`` so a crash midway leaves the vault
diagnosable rather than half-formed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

SCHEMA_VERSION: Final[int] = 2

_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
)

_TABLES: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _migration_steps (
        schema_target  INTEGER NOT NULL,
        step_name      TEXT NOT NULL,
        applied_at     INTEGER NOT NULL,
        PRIMARY KEY (schema_target, step_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        id           INTEGER PRIMARY KEY,
        external_id  TEXT NOT NULL UNIQUE,
        name         TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS embedding_models (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        dim        INTEGER NOT NULL,
        added_at   INTEGER NOT NULL,
        is_active  INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS embedding_models_single_active
        ON embedding_models(is_active) WHERE is_active = 1
    """,
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
    """,
    """
    CREATE INDEX IF NOT EXISTS facets_agent_type
        ON facets(agent_id, facet_type, captured_at DESC)
        WHERE is_deleted = 0
    """,
    """
    CREATE INDEX IF NOT EXISTS facets_captured
        ON facets(captured_at DESC) WHERE is_deleted = 0
    """,
    """
    CREATE INDEX IF NOT EXISTS facets_mode
        ON facets(mode, facet_type) WHERE is_deleted = 0
    """,
    """
    CREATE INDEX IF NOT EXISTS facets_embed_model
        ON facets(embed_model_id) WHERE is_deleted = 0
    """,
    """
    CREATE INDEX IF NOT EXISTS facets_embed_status
        ON facets(embed_status, embed_last_attempt_at)
        WHERE is_deleted = 0 AND embed_status IN ('pending', 'failed')
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS facets_fts USING fts5(
        content,
        content=facets,
        content_rowid=id,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS facets_ai AFTER INSERT ON facets BEGIN
        INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS facets_ad AFTER DELETE ON facets BEGIN
        INSERT INTO facets_fts(facets_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS facets_au AFTER UPDATE OF content ON facets BEGIN
        INSERT INTO facets_fts(facets_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        INSERT INTO facets_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
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
    """,
    """
    CREATE INDEX IF NOT EXISTS compiled_agent_type
        ON compiled_artifacts(agent_id, artifact_type, compiled_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS capabilities (
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
    CREATE INDEX IF NOT EXISTS capabilities_agent
        ON capabilities(agent_id, revoked_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS capabilities_expires
        ON capabilities(expires_at) WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id                  INTEGER PRIMARY KEY,
        at                  INTEGER NOT NULL,
        actor               TEXT NOT NULL,
        agent_id            INTEGER REFERENCES agents(id),
        op                  TEXT NOT NULL,
        target_external_id  TEXT,
        payload             TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at DESC)
    """,
)


def iter_pragmas() -> Iterator[str]:
    yield from _PRAGMAS


def iter_ddl() -> Iterator[str]:
    for stmt in _TABLES:
        yield _dedent(stmt)


def all_statements() -> list[str]:
    return [*_PRAGMAS, *(_dedent(s) for s in _TABLES)]


def _dedent(sql: str) -> str:
    return "\n".join(line.rstrip() for line in sql.strip().splitlines())
