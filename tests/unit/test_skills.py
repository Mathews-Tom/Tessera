"""Skills CRUD + disk-sync round-trip."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tessera.vault import audit, schema, skills


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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("git rebase", "git-rebase"),
        ("Git Rebase!", "git-rebase"),
        ("café au lait", "cafe-au-lait"),
        ("  spaces   in   name  ", "spaces-in-name"),
        ("under_scores", "under-scores"),
        ("ALLCAPS123", "allcaps123"),
    ],
)
def test_slugify_normalizes(name: str, expected: str) -> None:
    assert skills.slugify(name) == expected


@pytest.mark.unit
def test_slugify_rejects_unrepresentable_input() -> None:
    with pytest.raises(skills.SkillsError, match="no slug"):
        skills.slugify("!!!")


@pytest.mark.unit
def test_create_skill_persists_metadata(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, is_new = skills.create_skill(
        conn,
        agent_id=aid,
        name="git-rebase",
        description="Squash branches before merge",
        procedure_md="# Procedure\n\nUse interactive rebase.",
        source_tool="cli",
    )
    assert is_new is True
    skill = skills.get_by_external_id(conn, eid)
    assert skill is not None
    assert skill.name == "git-rebase"
    assert skill.description == "Squash branches before merge"
    assert skill.active is True
    assert skill.procedure_md.startswith("# Procedure")


@pytest.mark.unit
def test_create_skill_rejects_duplicate_name(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="git-rebase",
        description="A",
        procedure_md="alpha",
        source_tool="cli",
    )
    with pytest.raises(skills.DuplicateSkillNameError):
        skills.create_skill(
            conn,
            agent_id=aid,
            name="git-rebase",
            description="B",
            procedure_md="beta",
            source_tool="cli",
        )


@pytest.mark.unit
def test_create_skill_rejects_empty_name(conn: sqlite3.Connection) -> None:
    with pytest.raises(skills.SkillsError, match="non-empty"):
        skills.create_skill(
            conn,
            agent_id=_agent_id(conn),
            name="   ",
            description="x",
            procedure_md="y",
            source_tool="cli",
        )


@pytest.mark.unit
def test_get_by_name_returns_live_skill_only(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="git-rebase",
        description="x",
        procedure_md="alpha",
        source_tool="cli",
    )
    found = skills.get_by_name(conn, agent_id=aid, name="git-rebase")
    assert found is not None
    assert found.external_id == eid
    # Soft-delete the skill; lookup must miss.
    conn.execute("UPDATE facets SET is_deleted = 1 WHERE external_id = ?", (eid,))
    assert skills.get_by_name(conn, agent_id=aid, name="git-rebase") is None


@pytest.mark.unit
def test_list_skills_orders_by_name(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="charlie",
        description="x",
        procedure_md="c",
        source_tool="cli",
    )
    skills.create_skill(
        conn,
        agent_id=aid,
        name="alpha",
        description="x",
        procedure_md="a",
        source_tool="cli",
    )
    skills.create_skill(
        conn,
        agent_id=aid,
        name="bravo",
        description="x",
        procedure_md="b",
        source_tool="cli",
    )
    listed = skills.list_skills(conn, agent_id=aid)
    assert [s.name for s in listed] == ["alpha", "bravo", "charlie"]


@pytest.mark.unit
def test_list_skills_active_only_filters_inactive(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="retired",
        description="x",
        procedure_md="r",
        source_tool="cli",
    )
    skills.update_metadata(conn, external_id=eid, active=False)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="active",
        description="x",
        procedure_md="a",
        source_tool="cli",
    )
    active = skills.list_skills(conn, agent_id=aid, active_only=True)
    assert [s.name for s in active] == ["active"]
    everyone = skills.list_skills(conn, agent_id=aid, active_only=False)
    assert {s.name for s in everyone} == {"active", "retired"}


@pytest.mark.unit
def test_update_procedure_changes_hash_and_resets_embed(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="x",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    # Pretend the embed worker has already embedded it.
    conn.execute(
        "UPDATE facets SET embed_status = 'embedded', embed_attempts = 1 WHERE external_id = ?",
        (eid,),
    )
    changed = skills.update_procedure(conn, external_id=eid, procedure_md="beta")
    assert changed is True
    refreshed = skills.get_by_external_id(conn, eid)
    assert refreshed is not None
    assert refreshed.procedure_md == "beta"
    assert refreshed.embed_status == "pending"


@pytest.mark.unit
def test_update_procedure_is_noop_when_unchanged(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="x",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    assert skills.update_procedure(conn, external_id=eid, procedure_md="alpha") is False


@pytest.mark.unit
def test_update_procedure_rejects_collision_with_other_skill(
    conn: sqlite3.Connection,
) -> None:
    """Two skills cannot share the same content body — UNIQUE(agent_id, content_hash)."""

    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="alpha",
        description="d",
        procedure_md="shared",
        source_tool="cli",
    )
    other_eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="bravo",
        description="d",
        procedure_md="unique",
        source_tool="cli",
    )
    with pytest.raises(skills.SkillContentNotUniqueError):
        skills.update_procedure(conn, external_id=other_eid, procedure_md="shared")


@pytest.mark.unit
def test_update_metadata_field_audit_lists_changed_fields(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="x",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    changed = skills.update_metadata(conn, external_id=eid, name="y", description="updated")
    assert changed is True
    row = conn.execute(
        "SELECT payload FROM audit_log WHERE op = 'skill_metadata_updated' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload == {"fields_changed": ["description", "name"]}


@pytest.mark.unit
def test_update_metadata_rejects_rename_to_existing_name(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="taken",
        description="d",
        procedure_md="t",
        source_tool="cli",
    )
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="free",
        description="d",
        procedure_md="f",
        source_tool="cli",
    )
    with pytest.raises(skills.DuplicateSkillNameError):
        skills.update_metadata(conn, external_id=eid, name="taken")


@pytest.mark.unit
def test_update_metadata_noop_when_values_unchanged(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="x",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    assert skills.update_metadata(conn, external_id=eid, name="x", active=True) is False


@pytest.mark.unit
def test_set_disk_path_collision_raises(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    a_eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="a",
        description="d",
        procedure_md="a",
        source_tool="cli",
    )
    b_eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="b",
        description="d",
        procedure_md="b",
        source_tool="cli",
    )
    skills.set_disk_path(conn, external_id=a_eid, disk_path="/tmp/skills/a.md")
    with pytest.raises(skills.DiskPathCollisionError):
        skills.set_disk_path(conn, external_id=b_eid, disk_path="/tmp/skills/a.md")


@pytest.mark.unit
def test_set_disk_path_clear_writes_distinct_audit_op(conn: sqlite3.Connection) -> None:
    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="x",
        description="d",
        procedure_md="a",
        source_tool="cli",
    )
    skills.set_disk_path(conn, external_id=eid, disk_path="/tmp/x.md")
    skills.set_disk_path(conn, external_id=eid, disk_path=None)
    ops = [
        r[0]
        for r in conn.execute(
            "SELECT op FROM audit_log WHERE op LIKE 'skill_disk_path%' ORDER BY id"
        )
    ]
    assert ops == ["skill_disk_path_set", "skill_disk_path_cleared"]


@pytest.mark.unit
def test_sync_to_disk_writes_files_for_unsynced_skills(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="git rebase",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    skills.create_skill(
        conn,
        agent_id=aid,
        name="docker compose",
        description="d",
        procedure_md="beta",
        source_tool="cli",
    )
    report = skills.sync_to_disk(conn, agent_id=aid, base_dir=tmp_path)
    assert report.written == 2
    assert report.skipped == 0
    written_paths = {Path(p).name for p in report.paths}
    assert written_paths == {"git-rebase.md", "docker-compose.md"}
    # disk_path is now persisted so a re-sync skips both.
    rerun = skills.sync_to_disk(conn, agent_id=aid, base_dir=tmp_path)
    assert rerun.written == 0
    assert rerun.skipped == 2


@pytest.mark.unit
def test_sync_to_disk_overwrites_when_vault_diverges(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Vault wins when sync_to_disk sees a stale file body."""

    aid = _agent_id(conn)
    eid, _ = skills.create_skill(
        conn,
        agent_id=aid,
        name="git rebase",
        description="d",
        procedure_md="vault-version",
        source_tool="cli",
    )
    skills.set_disk_path(conn, external_id=eid, disk_path=str(tmp_path / "git-rebase.md"))
    (tmp_path / "git-rebase.md").write_text("disk-version", encoding="utf-8")
    report = skills.sync_to_disk(conn, agent_id=aid, base_dir=tmp_path)
    assert report.written == 1
    assert (tmp_path / "git-rebase.md").read_text(encoding="utf-8") == "vault-version"


@pytest.mark.unit
def test_sync_to_disk_appends_suffix_on_slug_collision(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two skills that slugify to the same stem must not collide on path."""

    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="git rebase",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    skills.create_skill(
        conn,
        agent_id=aid,
        name="git-rebase!",
        description="d",
        procedure_md="beta",
        source_tool="cli",
    )
    report = skills.sync_to_disk(conn, agent_id=aid, base_dir=tmp_path)
    paths = {Path(p).name for p in report.paths}
    assert "git-rebase.md" in paths
    assert "git-rebase-2.md" in paths


@pytest.mark.unit
def test_sync_from_disk_imports_new_files(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    (tmp_path / "git-rebase.md").write_text("# rebase procedure", encoding="utf-8")
    (tmp_path / "docker-compose.md").write_text("# compose procedure", encoding="utf-8")
    report = skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    assert report.imported == 2
    assert report.updated == 0
    assert report.unchanged == 0
    listed = skills.list_skills(conn, agent_id=aid)
    assert {s.name for s in listed} == {"git rebase", "docker compose"}
    # The disk_path column is set so the next sweep recognises them.
    assert all(s.disk_path is not None for s in listed)


@pytest.mark.unit
def test_sync_from_disk_updates_changed_body(conn: sqlite3.Connection, tmp_path: Path) -> None:
    aid = _agent_id(conn)
    path = tmp_path / "git-rebase.md"
    path.write_text("v1", encoding="utf-8")
    skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    path.write_text("v2", encoding="utf-8")
    report = skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    assert report.imported == 0
    assert report.updated == 1
    assert report.unchanged == 0


@pytest.mark.unit
def test_sync_from_disk_marks_unchanged_when_bytes_match(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    (tmp_path / "x.md").write_text("v1", encoding="utf-8")
    skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    report = skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    assert report.unchanged == 1
    assert report.updated == 0
    assert report.imported == 0


@pytest.mark.unit
def test_sync_from_disk_returns_empty_report_when_dir_absent(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    aid = _agent_id(conn)
    report = skills.sync_from_disk(
        conn, agent_id=aid, base_dir=tmp_path / "nonexistent", source_tool="import"
    )
    assert report == skills.SyncFromDiskReport()


@pytest.mark.unit
def test_round_trip_to_disk_then_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The full v0.3 spec round-trip: vault -> disk -> reread -> identical state."""

    aid = _agent_id(conn)
    skills.create_skill(
        conn,
        agent_id=aid,
        name="git rebase",
        description="d",
        procedure_md="alpha",
        source_tool="cli",
    )
    to_report = skills.sync_to_disk(conn, agent_id=aid, base_dir=tmp_path)
    assert to_report.written == 1
    from_report = skills.sync_from_disk(conn, agent_id=aid, base_dir=tmp_path, source_tool="import")
    assert from_report.unchanged == 1
    assert from_report.updated == 0
    assert from_report.imported == 0


@pytest.mark.unit
def test_audit_ops_are_registered() -> None:
    expected = {
        "skill_procedure_updated",
        "skill_metadata_updated",
        "skill_disk_path_set",
        "skill_disk_path_cleared",
    }
    assert expected <= audit.allowed_ops()
