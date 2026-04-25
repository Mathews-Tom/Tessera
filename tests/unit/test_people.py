"""People CRUD: insert, alias maintenance, merge/split, resolution, mentions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from tessera.vault import audit, people, schema


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    for stmt in schema.all_statements():
        c.execute(stmt)
    c.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01A', 'tom', 1)")
    yield c
    c.close()


def _agent_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM agents WHERE external_id = '01A'").fetchone()
    return int(row[0])


def _make_facet(conn: sqlite3.Connection, agent_id: int, *, external_id: str = "01F") -> str:
    """Insert a project facet directly and return its external_id."""

    conn.execute(
        """
        INSERT INTO facets(external_id, agent_id, facet_type, content,
                           content_hash, source_tool, captured_at)
        VALUES (?, ?, 'project', ?, ?, 'cli', 100)
        """,
        (external_id, agent_id, f"content for {external_id}", f"h-{external_id}"),
    )
    return external_id


@pytest.mark.unit
def test_insert_returns_new_id_for_first_write(conn: sqlite3.Connection) -> None:
    eid, is_new = people.insert(conn, agent_id=_agent_id(conn), canonical_name="Sarah Johnson")
    assert is_new is True
    assert eid.startswith("0")


@pytest.mark.unit
def test_insert_dedups_on_canonical_name(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    first, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    second, is_new = people.insert(conn, agent_id=aid, canonical_name="  Sarah  Johnson  ")
    assert second == first
    assert is_new is False


@pytest.mark.unit
def test_insert_dedup_merges_new_aliases_into_existing_row(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah", aliases=["S."])
    again, is_new = people.insert(conn, agent_id=aid, canonical_name="Sarah", aliases=["SJ"])
    assert again == eid
    assert is_new is False
    refreshed = people.get(conn, eid)
    assert refreshed is not None
    assert set(refreshed.aliases) == {"S.", "SJ"}


@pytest.mark.unit
def test_insert_normalizes_whitespace_and_nfc(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    e1, _ = people.insert(conn, agent_id=aid, canonical_name="café")
    e2, is_new = people.insert(conn, agent_id=aid, canonical_name="café")
    assert e2 == e1
    assert is_new is False


@pytest.mark.unit
def test_insert_rejects_empty_canonical_name(conn: sqlite3.Connection) -> None:
    with pytest.raises(people.PeopleError, match="non-empty"):
        people.insert(conn, agent_id=_agent_id(conn), canonical_name="   ")


@pytest.mark.unit
def test_insert_rejects_unknown_agent(conn: sqlite3.Connection) -> None:
    with pytest.raises(people.PeopleError, match="no agent"):
        people.insert(conn, agent_id=99999, canonical_name="Ghost")


@pytest.mark.unit
def test_insert_emits_audit_with_alias_count_only(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah", aliases=["S.", "SJ"])
    row = conn.execute(
        "SELECT op, target_external_id, payload FROM audit_log WHERE op = 'person_created'"
    ).fetchone()
    assert row is not None
    assert row[1] == eid
    payload = json.loads(row[2])
    assert payload == {"alias_count": 2}
    # canonical_name and aliases must not leak into the audit payload.
    assert "Sarah" not in row[2]


@pytest.mark.unit
def test_get_returns_none_for_missing(conn: sqlite3.Connection) -> None:
    assert people.get(conn, "01NOTHING") is None


@pytest.mark.unit
def test_get_by_canonical_name_is_normalization_aware(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    found = people.get_by_canonical_name(conn, agent_id=aid, canonical_name="  Sarah  Johnson  ")
    assert found is not None
    assert found.external_id == eid


@pytest.mark.unit
def test_list_by_agent_orders_by_canonical_name(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Charlie", created_at=300)
    people.insert(conn, agent_id=aid, canonical_name="Alice", created_at=100)
    people.insert(conn, agent_id=aid, canonical_name="Bob", created_at=200)
    listed = people.list_by_agent(conn, agent_id=aid)
    assert [p.canonical_name for p in listed] == ["Alice", "Bob", "Charlie"]


@pytest.mark.unit
def test_add_alias_appends_and_dedupes(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    assert people.add_alias(conn, external_id=eid, alias="S.") is True
    assert people.add_alias(conn, external_id=eid, alias="S.") is False
    refreshed = people.get(conn, eid)
    assert refreshed is not None
    assert refreshed.aliases == ("S.",)


@pytest.mark.unit
def test_add_alias_raises_for_unknown_person(conn: sqlite3.Connection) -> None:
    with pytest.raises(people.UnknownPersonError):
        people.add_alias(conn, external_id="01MISSING", alias="x")


@pytest.mark.unit
def test_merge_collapses_aliases_and_mentions(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    primary_eid, _ = people.insert(
        conn, agent_id=aid, canonical_name="Sarah Johnson", aliases=["Sarah"]
    )
    secondary_eid, _ = people.insert(
        conn, agent_id=aid, canonical_name="S. Johnson", aliases=["SJ"]
    )
    f1 = _make_facet(conn, aid, external_id="01F1")
    f2 = _make_facet(conn, aid, external_id="01F2")
    people.link_facet_mention(conn, facet_external_id=f1, person_external_id=primary_eid)
    people.link_facet_mention(conn, facet_external_id=f2, person_external_id=secondary_eid)

    survivor = people.merge(
        conn, primary_external_id=primary_eid, secondary_external_id=secondary_eid
    )
    assert {"Sarah", "SJ", "S. Johnson"} <= set(survivor.aliases)
    assert people.get(conn, secondary_eid) is None
    # Both facets now point at the survivor.
    f1_people = [p.external_id for p, _ in people.people_for_facet(conn, facet_external_id=f1)]
    f2_people = [p.external_id for p, _ in people.people_for_facet(conn, facet_external_id=f2)]
    assert f1_people == [primary_eid]
    assert f2_people == [primary_eid]


@pytest.mark.unit
def test_merge_drops_secondary_link_when_primary_already_linked(
    conn: sqlite3.Connection,
) -> None:
    """The OR IGNORE path: if a facet was linked to both rows, the survivor
    keeps its existing row and the secondary's duplicate is dropped."""

    aid = _agent_id(conn)
    primary_eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    secondary_eid, _ = people.insert(conn, agent_id=aid, canonical_name="S. Johnson")
    f1 = _make_facet(conn, aid, external_id="01F1")
    people.link_facet_mention(conn, facet_external_id=f1, person_external_id=primary_eid)
    people.link_facet_mention(
        conn, facet_external_id=f1, person_external_id=secondary_eid, confidence=0.5
    )
    people.merge(conn, primary_external_id=primary_eid, secondary_external_id=secondary_eid)
    # Only one mention row survives — the original primary's, with confidence 1.0.
    rows = conn.execute("SELECT confidence FROM person_mentions").fetchall()
    assert len(rows) == 1
    assert float(rows[0][0]) == 1.0


@pytest.mark.unit
def test_merge_rejects_self_merge(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    with pytest.raises(people.PeopleError, match="into itself"):
        people.merge(conn, primary_external_id=eid, secondary_external_id=eid)


@pytest.mark.unit
def test_merge_rejects_unknown_person(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    with pytest.raises(people.UnknownPersonError):
        people.merge(conn, primary_external_id=eid, secondary_external_id="01GHOST")


@pytest.mark.unit
def test_split_extracts_aliases_into_new_person(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(
        conn, agent_id=aid, canonical_name="Sarah", aliases=["Sarah J", "Sarah Johnson"]
    )
    original, new = people.split(
        conn,
        person_external_id=eid,
        extracted_canonical_name="Sarah Johnson",
        move_aliases=["Sarah J"],
    )
    assert new.canonical_name == "Sarah Johnson"
    assert "Sarah J" in new.aliases
    assert "Sarah J" not in original.aliases


@pytest.mark.unit
def test_split_rejects_same_canonical(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    with pytest.raises(people.PeopleError, match="must differ"):
        people.split(conn, person_external_id=eid, extracted_canonical_name="Sarah")


@pytest.mark.unit
def test_split_rejects_existing_canonical(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    with pytest.raises(people.DuplicateCanonicalNameError):
        people.split(conn, person_external_id=eid, extracted_canonical_name="Sarah Johnson")


@pytest.mark.unit
def test_resolve_exact_canonical_match_is_marked_exact(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson", aliases=["Sarah"])
    result = people.resolve(conn, agent_id=aid, mention="Sarah Johnson")
    assert result.is_exact is True
    assert len(result.matches) == 1


@pytest.mark.unit
def test_resolve_is_case_insensitive(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    result = people.resolve(conn, agent_id=aid, mention="sarah johnson")
    assert result.is_exact is True
    assert result.matches[0].canonical_name == "Sarah Johnson"


@pytest.mark.unit
def test_resolve_exact_alias_match(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson", aliases=["SJ"])
    result = people.resolve(conn, agent_id=aid, mention="SJ")
    assert result.is_exact is True
    assert result.matches[0].external_id == eid


@pytest.mark.unit
def test_resolve_prefix_match_returns_multiple_unflagged(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    people.insert(conn, agent_id=aid, canonical_name="Sarah Kim")
    result = people.resolve(conn, agent_id=aid, mention="Sarah")
    assert result.is_exact is False
    canonical_names = {p.canonical_name for p in result.matches}
    assert canonical_names == {"Sarah Johnson", "Sarah Kim"}


@pytest.mark.unit
def test_resolve_no_match_returns_empty(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Sarah Johnson")
    result = people.resolve(conn, agent_id=aid, mention="Marcus")
    assert result.is_exact is False
    assert result.matches == ()


@pytest.mark.unit
def test_link_facet_mention_is_idempotent(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    assert people.link_facet_mention(conn, facet_external_id=f1, person_external_id=eid) is True
    assert people.link_facet_mention(conn, facet_external_id=f1, person_external_id=eid) is False


@pytest.mark.unit
def test_link_facet_mention_rejects_out_of_range_confidence(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    with pytest.raises(people.PeopleError, match=r"\[0.0, 1.0\]"):
        people.link_facet_mention(
            conn, facet_external_id=f1, person_external_id=eid, confidence=1.5
        )


@pytest.mark.unit
def test_link_facet_mention_rejects_unknown_facet(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    with pytest.raises(people.UnknownFacetError):
        people.link_facet_mention(conn, facet_external_id="01MISSING", person_external_id=eid)


@pytest.mark.unit
def test_unlink_facet_mention_returns_false_when_link_absent(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    assert people.unlink_facet_mention(conn, facet_external_id=f1, person_external_id=eid) is False


@pytest.mark.unit
def test_facets_for_person_excludes_soft_deleted(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    live = _make_facet(conn, aid, external_id="01LIVE")
    dead = _make_facet(conn, aid, external_id="01DEAD")
    people.link_facet_mention(conn, facet_external_id=live, person_external_id=eid)
    people.link_facet_mention(conn, facet_external_id=dead, person_external_id=eid)
    conn.execute("UPDATE facets SET is_deleted = 1 WHERE external_id = ?", (dead,))
    listed = [fid for fid, _ in people.facets_for_person(conn, person_external_id=eid)]
    assert listed == [live]


@pytest.mark.unit
def test_facet_delete_cascades_to_mentions(conn: sqlite3.Connection) -> None:
    """Hard-deleting a facet must drop its person_mentions rows."""

    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    people.link_facet_mention(conn, facet_external_id=f1, person_external_id=eid)
    conn.execute("DELETE FROM facets WHERE external_id = ?", (f1,))
    count = conn.execute("SELECT COUNT(*) FROM person_mentions").fetchone()[0]
    assert int(count) == 0


@pytest.mark.unit
def test_person_delete_cascades_to_mentions(conn: sqlite3.Connection) -> None:
    """Hard-deleting a person row must drop its person_mentions rows."""

    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    people.link_facet_mention(conn, facet_external_id=f1, person_external_id=eid)
    conn.execute("DELETE FROM people WHERE external_id = ?", (eid,))
    count = conn.execute("SELECT COUNT(*) FROM person_mentions").fetchone()[0]
    assert int(count) == 0


@pytest.mark.unit
def test_audit_ops_are_registered_in_allowlist() -> None:
    expected = {
        "person_created",
        "person_alias_added",
        "person_merged",
        "person_split",
        "person_mention_linked",
        "person_mention_unlinked",
    }
    assert expected <= audit.allowed_ops()


@pytest.mark.unit
def test_add_alias_empty_string_is_noop(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    assert people.add_alias(conn, external_id=eid, alias="   ") is False


@pytest.mark.unit
def test_get_by_canonical_name_returns_none_when_missing(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    assert people.get_by_canonical_name(conn, agent_id=aid, canonical_name="Ghost") is None


@pytest.mark.unit
def test_list_by_agent_respects_since(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Old", created_at=100)
    people.insert(conn, agent_id=aid, canonical_name="New", created_at=300)
    listed = people.list_by_agent(conn, agent_id=aid, since=200)
    assert [p.canonical_name for p in listed] == ["New"]


@pytest.mark.unit
def test_unlink_raises_for_unknown_facet(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    with pytest.raises(people.UnknownFacetError):
        people.unlink_facet_mention(conn, facet_external_id="01MISSING", person_external_id=eid)


@pytest.mark.unit
def test_unlink_raises_for_unknown_person(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    f1 = _make_facet(conn, aid)
    with pytest.raises(people.UnknownPersonError):
        people.unlink_facet_mention(conn, facet_external_id=f1, person_external_id="01MISSING")


@pytest.mark.unit
def test_unlink_emits_audit_when_link_removed(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    f1 = _make_facet(conn, aid)
    people.link_facet_mention(conn, facet_external_id=f1, person_external_id=eid)
    assert people.unlink_facet_mention(conn, facet_external_id=f1, person_external_id=eid) is True
    row = conn.execute("SELECT op FROM audit_log WHERE op = 'person_mention_unlinked'").fetchone()
    assert row is not None


@pytest.mark.unit
def test_link_raises_for_unknown_person(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    f1 = _make_facet(conn, aid)
    with pytest.raises(people.UnknownPersonError):
        people.link_facet_mention(conn, facet_external_id=f1, person_external_id="01MISSING")


@pytest.mark.unit
def test_merge_rejects_cross_agent_people(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01B', 'b', 1)")
    other_aid = int(conn.execute("SELECT id FROM agents WHERE external_id = '01B'").fetchone()[0])
    primary_eid, _ = people.insert(conn, agent_id=aid, canonical_name="Sarah")
    secondary_eid, _ = people.insert(conn, agent_id=other_aid, canonical_name="Sarah")
    with pytest.raises(people.PeopleError, match="agent boundaries"):
        people.merge(
            conn,
            primary_external_id=primary_eid,
            secondary_external_id=secondary_eid,
        )


@pytest.mark.unit
def test_resolve_empty_mention_returns_empty(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    people.insert(conn, agent_id=aid, canonical_name="Sarah")
    result = people.resolve(conn, agent_id=aid, mention="   ")
    assert result.matches == ()
    assert result.is_exact is False


@pytest.mark.unit
def test_people_for_facet_ordered_by_confidence(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    high_eid, _ = people.insert(conn, agent_id=aid, canonical_name="High")
    low_eid, _ = people.insert(conn, agent_id=aid, canonical_name="Low")
    f1 = _make_facet(conn, aid)
    people.link_facet_mention(
        conn, facet_external_id=f1, person_external_id=low_eid, confidence=0.3
    )
    people.link_facet_mention(
        conn, facet_external_id=f1, person_external_id=high_eid, confidence=0.9
    )
    ordered = people.people_for_facet(conn, facet_external_id=f1)
    assert [p.canonical_name for p, _ in ordered] == ["High", "Low"]
