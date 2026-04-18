"""Schema DDL compiles and triggers cascade as specified."""

from __future__ import annotations

import sqlite3

import pytest

from tessera.vault import schema


@pytest.mark.unit
def test_schema_version_is_one() -> None:
    assert schema.SCHEMA_VERSION == 1


@pytest.mark.unit
def test_all_statements_apply_on_plain_sqlite() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agents", "facets", "capabilities", "audit_log", "embedding_models", "_meta"} <= tables
    assert "_migration_steps" in tables
    assert "facets_fts" in tables


@pytest.mark.unit
def test_facet_type_check_constraint_rejects_unknown_type() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_client, captured_at)
            VALUES ('f1', 1, 'not_a_type', 'x', 'h', 'cli', 1)
            """
        )


@pytest.mark.unit
def test_embedding_models_unique_active_constraint() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO embedding_models(name, dim, added_at, is_active) VALUES (?, ?, ?, ?)",
        ("m1", 768, 1, 1),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO embedding_models(name, dim, added_at, is_active) VALUES (?, ?, ?, ?)",
            ("m2", 768, 2, 1),
        )


@pytest.mark.unit
def test_fts_trigger_syncs_facets_on_insert_update_delete() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_client, captured_at)
        VALUES ('f1', 1, 'semantic', 'alpha beta', 'h1', 'cli', 1)
        """
    )
    assert conn.execute("SELECT content FROM facets_fts").fetchone() == ("alpha beta",)

    conn.execute("UPDATE facets SET content = 'gamma delta' WHERE external_id = 'f1'")
    assert conn.execute("SELECT content FROM facets_fts").fetchone() == ("gamma delta",)

    conn.execute("DELETE FROM facets WHERE external_id = 'f1'")
    assert conn.execute("SELECT content FROM facets_fts").fetchall() == []


@pytest.mark.unit
def test_facets_unique_agent_content_hash() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_client, captured_at)
        VALUES ('f1', 1, 'semantic', 'x', 'h', 'cli', 1)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_client, captured_at)
            VALUES ('f2', 1, 'semantic', 'y', 'h', 'cli', 2)
            """
        )


@pytest.mark.unit
def test_foreign_keys_enforced_when_pragma_enabled() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_client, captured_at)
            VALUES ('f1', 999, 'semantic', 'x', 'h', 'cli', 1)
            """
        )
