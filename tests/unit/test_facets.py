"""Facets CRUD: dedup, soft/hard delete, FTS cascade."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from tessera.vault import facets, schema


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        c.execute(stmt)
    c.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'test', 1)")
    yield c
    c.close()


def _agent_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM agents WHERE external_id = '01A'").fetchone()
    return int(row[0])


@pytest.mark.unit
def test_content_hash_is_stable_across_nfc_and_whitespace() -> None:
    assert facets.content_hash(" hello ") == facets.content_hash("hello")
    assert facets.content_hash("café") == facets.content_hash("cafe\u0301")


@pytest.mark.unit
def test_content_hash_preserves_case_and_internal_whitespace() -> None:
    assert facets.content_hash("Hello World") != facets.content_hash("hello world")
    assert facets.content_hash("foo bar") != facets.content_hash("foo  bar")


@pytest.mark.unit
def test_insert_returns_new_external_id_on_first_write(conn: sqlite3.Connection) -> None:
    external_id, is_new = facets.insert(
        conn,
        agent_id=_agent_id(conn),
        facet_type="semantic",
        content="the sky is blue",
        source_client="cli",
    )
    assert is_new is True
    assert external_id.startswith("0")


@pytest.mark.unit
def test_insert_dedups_on_same_agent_and_normalized_content(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    first, _ = facets.insert(
        conn, agent_id=aid, facet_type="semantic", content="hi", source_client="cli"
    )
    second, is_new = facets.insert(
        conn, agent_id=aid, facet_type="semantic", content=" hi ", source_client="cli"
    )
    assert second == first
    assert is_new is False


@pytest.mark.unit
def test_insert_rejects_unsupported_facet_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(facets.UnsupportedFacetTypeError):
        facets.insert(
            conn,
            agent_id=_agent_id(conn),
            facet_type="skill",
            content="x",
            source_client="cli",
        )


@pytest.mark.unit
def test_insert_rejects_unknown_agent(conn: sqlite3.Connection) -> None:
    with pytest.raises(facets.UnknownAgentError):
        facets.insert(
            conn,
            agent_id=99999,
            facet_type="semantic",
            content="x",
            source_client="cli",
        )


@pytest.mark.unit
def test_get_returns_none_for_missing_id(conn: sqlite3.Connection) -> None:
    assert facets.get(conn, "01NOTHING") is None


@pytest.mark.unit
def test_list_by_type_orders_by_captured_at_desc(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="episodic",
        content="one",
        source_client="cli",
        captured_at=100,
    )
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="episodic",
        content="two",
        source_client="cli",
        captured_at=200,
    )
    results = facets.list_by_type(conn, agent_id=aid, facet_type="episodic")
    assert [f.content for f in results] == ["two", "one"]


@pytest.mark.unit
def test_list_by_type_respects_since_filter(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="episodic",
        content="old",
        source_client="cli",
        captured_at=100,
    )
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="episodic",
        content="new",
        source_client="cli",
        captured_at=200,
    )
    results = facets.list_by_type(conn, agent_id=aid, facet_type="episodic", since=150)
    assert [f.content for f in results] == ["new"]


@pytest.mark.unit
def test_list_by_type_excludes_soft_deleted(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(conn, agent_id=aid, facet_type="style", content="x", source_client="cli")
    facets.soft_delete(conn, eid)
    assert facets.list_by_type(conn, agent_id=aid, facet_type="style") == []


@pytest.mark.unit
def test_soft_delete_is_idempotent_and_preserves_row(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(conn, agent_id=aid, facet_type="style", content="x", source_client="cli")
    assert facets.soft_delete(conn, eid) is True
    assert facets.soft_delete(conn, eid) is False  # already deleted
    still = facets.get(conn, eid)
    assert still is not None
    assert still.is_deleted is True


@pytest.mark.unit
def test_hard_delete_cascades_to_fts(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(
        conn,
        agent_id=aid,
        facet_type="semantic",
        content="haystack content",
        source_client="cli",
    )
    assert conn.execute("SELECT COUNT(*) FROM facets_fts").fetchone()[0] == 1
    assert facets.hard_delete(conn, eid) is True
    assert conn.execute("SELECT COUNT(*) FROM facets_fts").fetchone()[0] == 0
    assert facets.get(conn, eid) is None


@pytest.mark.unit
def test_hard_delete_nonexistent_returns_false(conn: sqlite3.Connection) -> None:
    assert facets.hard_delete(conn, "01MISSING") is False


@pytest.mark.unit
def test_insert_after_soft_delete_restores_the_row(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(
        conn, agent_id=aid, facet_type="style", content="my voice sample", source_client="cli"
    )
    assert facets.soft_delete(conn, eid) is True

    eid2, is_new = facets.insert(
        conn, agent_id=aid, facet_type="style", content="my voice sample", source_client="cli"
    )
    assert eid2 == eid
    assert is_new is False
    restored = facets.get(conn, eid)
    assert restored is not None
    assert restored.is_deleted is False
    # The restored row is visible to read helpers again.
    listed = facets.list_by_type(conn, agent_id=aid, facet_type="style")
    assert [f.external_id for f in listed] == [eid]


@pytest.mark.unit
def test_v0_1_facet_types_are_subset_of_schema_check() -> None:
    """The application guard must never accept a type the schema rejects."""

    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'a', 1)")
    for ft in facets.V0_1_FACET_TYPES:
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_client, captured_at)
            VALUES (?, 1, ?, 'x', ?, 'cli', 1)
            """,
            (f"01{ft.upper()}", ft, ft),
        )
