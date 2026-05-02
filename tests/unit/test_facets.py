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
        facet_type="preference",
        content="the sky is blue",
        source_tool="cli",
    )
    assert is_new is True
    assert external_id.startswith("0")


@pytest.mark.unit
def test_insert_dedups_on_same_agent_and_normalized_content(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    first, _ = facets.insert(
        conn, agent_id=aid, facet_type="preference", content="hi", source_tool="cli"
    )
    second, is_new = facets.insert(
        conn, agent_id=aid, facet_type="preference", content=" hi ", source_tool="cli"
    )
    assert second == first
    assert is_new is False


@pytest.mark.unit
def test_insert_rejects_unsupported_facet_type(conn: sqlite3.Connection) -> None:
    # ``compiled_notebook`` is reserved in the schema CHECK but stays
    # outside the v0.3 writable set until v0.5 activates write-time
    # compilation.
    with pytest.raises(facets.UnsupportedFacetTypeError):
        facets.insert(
            conn,
            agent_id=_agent_id(conn),
            facet_type="compiled_notebook",
            content="x",
            source_tool="cli",
        )


@pytest.mark.unit
@pytest.mark.parametrize("facet_type", ["person", "skill"])
def test_insert_accepts_v0_3_unlocked_types(conn: sqlite3.Connection, facet_type: str) -> None:
    external_id, is_new = facets.insert(
        conn,
        agent_id=_agent_id(conn),
        facet_type=facet_type,
        content=f"test {facet_type}",
        source_tool="cli",
    )
    assert is_new is True
    fetched = facets.get(conn, external_id)
    assert fetched is not None
    assert fetched.facet_type == facet_type


@pytest.mark.unit
def test_insert_rejects_unknown_agent(conn: sqlite3.Connection) -> None:
    with pytest.raises(facets.UnknownAgentError):
        facets.insert(
            conn,
            agent_id=99999,
            facet_type="preference",
            content="x",
            source_tool="cli",
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
        facet_type="project",
        content="one",
        source_tool="cli",
        captured_at=100,
    )
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="project",
        content="two",
        source_tool="cli",
        captured_at=200,
    )
    results = facets.list_by_type(conn, agent_id=aid, facet_type="project")
    assert [f.content for f in results] == ["two", "one"]


@pytest.mark.unit
def test_list_by_type_respects_since_filter(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="project",
        content="old",
        source_tool="cli",
        captured_at=100,
    )
    facets.insert(
        conn,
        agent_id=aid,
        facet_type="project",
        content="new",
        source_tool="cli",
        captured_at=200,
    )
    results = facets.list_by_type(conn, agent_id=aid, facet_type="project", since=150)
    assert [f.content for f in results] == ["new"]


@pytest.mark.unit
def test_list_by_type_excludes_soft_deleted(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(conn, agent_id=aid, facet_type="style", content="x", source_tool="cli")
    facets.soft_delete(conn, eid)
    assert facets.list_by_type(conn, agent_id=aid, facet_type="style") == []


@pytest.mark.unit
def test_soft_delete_is_idempotent_and_preserves_row(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = facets.insert(conn, agent_id=aid, facet_type="style", content="x", source_tool="cli")
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
        facet_type="preference",
        content="haystack content",
        source_tool="cli",
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
        conn, agent_id=aid, facet_type="style", content="my voice sample", source_tool="cli"
    )
    assert facets.soft_delete(conn, eid) is True

    eid2, is_new = facets.insert(
        conn, agent_id=aid, facet_type="style", content="my voice sample", source_tool="cli"
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
def test_writable_facet_types_are_subset_of_schema_check() -> None:
    """The application guard must never accept a type the schema rejects."""

    conn = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        conn.execute(stmt)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'a', 1)")
    for ft in facets.WRITABLE_FACET_TYPES:
        conn.execute(
            """
            INSERT INTO facets(external_id, agent_id, facet_type, content,
                               content_hash, source_tool, captured_at)
            VALUES (?, 1, ?, 'x', ?, 'cli', 1)
            """,
            (f"01{ft.upper()}", ft, ft),
        )


@pytest.mark.unit
def test_writable_facet_types_match_active_v0_5_vocabulary() -> None:
    """V0.5-P2 unlocks ``agent_profile`` alongside the v0.3 set; the
    other v0.5-reserved types stay CHECK-permitted but write-rejected."""

    assert (
        facets.V0_1_FACET_TYPES | {"person", "skill", "agent_profile"}
        == facets.WRITABLE_FACET_TYPES
    )
    for reserved in (
        "compiled_notebook",
        "verification_checklist",
        "retrospective",
        "automation",
    ):
        assert reserved not in facets.WRITABLE_FACET_TYPES
        assert reserved in facets.ALL_FACET_TYPES
