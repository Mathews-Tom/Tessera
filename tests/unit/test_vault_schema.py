"""Schema DDL compiles and triggers cascade as specified."""

from __future__ import annotations

import sqlite3

import pytest

from tessera.vault import schema


@pytest.mark.unit
def test_schema_version_matches_current_surface() -> None:
    # v4 absorbs the v0.5 reconciliation cumulatively: V0.5-P1 added the
    # ADR-0016 memory volatility column / TTL / sweep index on top of the
    # v0.3 People + Skills surface; V0.5-P2 reserves the v0.5 facet
    # types (agent_profile, verification_checklist, retrospective,
    # automation) in the facet_type CHECK and adds the
    # ``agents.profile_facet_external_id`` FK linkage column. Schema
    # bump for v0.5 stays at 4 because every delta is additive.
    assert schema.SCHEMA_VERSION == 4


@pytest.mark.unit
def test_agents_carries_profile_facet_external_id_column() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    assert "profile_facet_external_id" in cols


@pytest.mark.unit
def test_agents_profile_link_accepts_null_default() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'tool', 1)")
    row = conn.execute(
        "SELECT profile_facet_external_id FROM agents WHERE external_id = 'a1'"
    ).fetchone()
    assert row[0] is None


@pytest.mark.unit
def test_all_statements_apply_on_plain_sqlite() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "agents",
        "facets",
        "capabilities",
        "audit_log",
        "embedding_models",
        "compiled_artifacts",
        "people",
        "person_mentions",
        "_meta",
    } <= tables
    assert "_migration_steps" in tables
    assert "facets_fts" in tables


@pytest.mark.unit
def test_facets_carries_disk_path_column() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(facets)").fetchall()}
    assert "disk_path" in cols


@pytest.mark.unit
def test_people_unique_canonical_name_per_agent() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO people(external_id, agent_id, canonical_name, created_at)
        VALUES ('p1', 1, 'Sarah Johnson', 1)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO people(external_id, agent_id, canonical_name, created_at)
            VALUES ('p2', 1, 'Sarah Johnson', 2)
            """
        )


@pytest.mark.unit
def test_person_mentions_unique_facet_person_pair() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, 'project', 'x', 'h', 'cli', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO people(external_id, agent_id, canonical_name, created_at)
        VALUES ('p1', 1, 'Sarah', 1)
        """
    )
    conn.execute("INSERT INTO person_mentions(facet_id, person_id, confidence) VALUES (1, 1, 0.9)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_mentions(facet_id, person_id, confidence) VALUES (1, 1, 1.0)"
        )


@pytest.mark.unit
def test_person_mentions_confidence_check_rejects_out_of_range() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, 'project', 'x', 'h', 'cli', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO people(external_id, agent_id, canonical_name, created_at)
        VALUES ('p1', 1, 'Sarah', 1)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_mentions(facet_id, person_id, confidence) VALUES (1, 1, 1.5)"
        )


@pytest.mark.unit
def test_disk_path_unique_per_agent_when_present() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at, disk_path)
        VALUES ('f1', 1, 'skill', 'x', 'h1', 'cli', 1, '/tmp/a.md')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at, disk_path)
            VALUES ('f2', 1, 'skill', 'y', 'h2', 'cli', 1, '/tmp/a.md')
            """
        )


@pytest.mark.unit
def test_disk_path_null_does_not_collide() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, 'project', 'x', 'h1', 'cli', 1)
        """
    )
    # A second NULL disk_path on a different facet must not collide via
    # the partial unique index.
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES ('f2', 1, 'project', 'y', 'h2', 'cli', 2)
        """
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "facet_type",
    [
        "identity",
        "preference",
        "workflow",
        "project",
        "style",
        "person",
        "skill",
        "compiled_notebook",
        "agent_profile",
        "verification_checklist",
        "retrospective",
        "automation",
    ],
)
def test_facet_type_check_accepts_adr_0010_vocabulary(facet_type: str) -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, ?, 'x', 'h', 'cli', 1)
        """,
        (facet_type,),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "retired",
    ["episodic", "semantic", "relationship", "goal", "judgment"],
)
def test_facet_type_check_rejects_retired_types(retired: str) -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES ('f1', 1, ?, 'x', 'h', 'cli', 1)
            """,
            (retired,),
        )


@pytest.mark.unit
def test_mode_check_rejects_unknown_mode() -> None:
    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('a1', 'a', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, mode, source_tool, captured_at)
            VALUES ('f1', 1, 'project', 'x', 'h', 'not_a_mode', 'cli', 1)
            """
        )


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
                               content_hash, source_tool, captured_at)
            VALUES ('f1', 1, 'not_a_type', 'x', 'h', 'cli', 1)
            """
        )


# Note: the v0.1 writable types are validated at the MCP boundary (see
# tests/unit/test_mcp_tool_validation.py); the schema CHECK is a safety
# net that additionally admits the reserved v0.3/v0.5 types so tokens
# granting reads on those types round-trip cleanly.


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
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, 'preference', 'alpha beta', 'h1', 'cli', 1)
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
                           content_hash, source_tool, captured_at)
        VALUES ('f1', 1, 'preference', 'x', 'h', 'cli', 1)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES ('f2', 1, 'preference', 'y', 'h', 'cli', 2)
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
                               content_hash, source_tool, captured_at)
            VALUES ('f1', 999, 'preference', 'x', 'h', 'cli', 1)
            """
        )
