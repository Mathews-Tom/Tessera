"""Skills CRUD with disk-sync round-trip.

Skills are facets with ``facet_type='skill'``: their ``content`` carries
the procedure markdown the user wrote, their ``metadata`` carries the
skill's ``name``, ``description``, and ``active`` flag, and their
optional ``disk_path`` column points at a ``.md`` file on disk when
the skill is synced. The schema-level partial unique index on
``(agent_id, disk_path)`` keeps each disk file mapped to at most one
skill row.

Two sync directions are supported. ``sync_to_disk`` walks every active
skill for an agent and emits a ``.md`` file per skill, assigning a
slugified ``disk_path`` on the first sync. ``sync_from_disk`` walks a
directory of ``.md`` files and either updates the matching skill (when
the path is already in ``disk_path``) or creates a new skill from the
filename stem. Both directions report counts and per-file errors.

The Phase 4 MCP tools (``learn_skill``, ``get_skill``, ``list_skills``)
delegate to this module; Phase 5's ``tessera skills`` CLI wraps the
sync operations.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlcipher3

from tessera.vault import audit
from tessera.vault import facets as vault_facets
from tessera.vault.facets import content_hash


class SkillsError(Exception):
    """Base class for skills-module failures."""


class UnknownSkillError(SkillsError):
    """Referenced skill external_id does not exist."""


class DuplicateSkillNameError(SkillsError):
    """A skill with this name already exists for the agent."""


class DiskPathCollisionError(SkillsError):
    """The requested disk_path is already in use by another skill."""


class SkillContentNotUniqueError(SkillsError):
    """Updating a skill to an existing skill's procedure markdown
    would violate UNIQUE(agent_id, content_hash). The caller decides
    whether to merge the duplicate or rename the conflicting skill."""


@dataclass(frozen=True, slots=True)
class Skill:
    facet_id: int
    external_id: str
    agent_id: int
    name: str
    description: str
    active: bool
    procedure_md: str
    content_hash: str
    disk_path: str | None
    captured_at: int
    embed_status: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SyncToDiskReport:
    written: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncFromDiskReport:
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


def slugify(name: str) -> str:
    """Slugify a skill name into a filesystem-safe stem.

    Lowercases, NFKD-normalizes to strip diacritics, replaces non-word
    runs with hyphens, and trims leading/trailing hyphens. Empty input
    after normalization is rejected so the caller is forced to supply
    a non-empty name.
    """

    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not hyphenated:
        raise SkillsError(f"name {name!r} has no slug-able characters")
    return hyphenated


def create_skill(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    name: str,
    description: str,
    procedure_md: str,
    source_tool: str,
    active: bool = True,
    captured_at: int | None = None,
) -> tuple[str, bool]:
    """Create a skill facet. Returns ``(external_id, is_new)``.

    Raises :class:`DuplicateSkillNameError` when a live skill with the
    same name already exists for this agent — names are user-facing
    identifiers and must be unique. Procedure dedup happens through the
    underlying ``facets`` UNIQUE(agent_id, content_hash) constraint;
    two skills cannot share the same procedure body.
    """

    name = name.strip()
    if not name:
        raise SkillsError("name must be non-empty")
    existing = get_by_name(conn, agent_id=agent_id, name=name)
    if existing is not None:
        raise DuplicateSkillNameError(f"skill named {name!r} already exists for agent {agent_id}")
    metadata = {"name": name, "description": description, "active": active}
    return vault_facets.insert(
        conn,
        agent_id=agent_id,
        facet_type="skill",
        content=procedure_md,
        source_tool=source_tool,
        metadata=metadata,
        captured_at=captured_at,
    )


def get_by_external_id(conn: sqlcipher3.Connection, external_id: str) -> Skill | None:
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, content_hash, captured_at,
               metadata, embed_status, disk_path, is_deleted
        FROM facets WHERE external_id = ? AND facet_type = 'skill'
        """,
        (external_id,),
    ).fetchone()
    if row is None or bool(row[9]):
        return None
    return _row_to_skill(row)


def get_by_name(conn: sqlcipher3.Connection, *, agent_id: int, name: str) -> Skill | None:
    """Look up a live skill by exact ``metadata.name`` match.

    Skill names are stored in JSON metadata; SQLite's json_extract
    handles the join. We filter for live rows so deleted skills don't
    collide with new ones of the same name.
    """

    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, content_hash, captured_at,
               metadata, embed_status, disk_path, is_deleted
        FROM facets
        WHERE agent_id = ? AND facet_type = 'skill' AND is_deleted = 0
          AND json_extract(metadata, '$.name') = ?
        LIMIT 1
        """,
        (agent_id, name.strip()),
    ).fetchone()
    if row is None:
        return None
    return _row_to_skill(row)


def list_skills(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    active_only: bool = True,
    limit: int = 50,
) -> list[Skill]:
    """List skill facets for an agent, ordered by name."""

    base = """
        SELECT id, external_id, agent_id, content, content_hash, captured_at,
               metadata, embed_status, disk_path, is_deleted
        FROM facets
        WHERE agent_id = ? AND facet_type = 'skill' AND is_deleted = 0
    """
    if active_only:
        base += " AND COALESCE(json_extract(metadata, '$.active'), 1) = 1"
    base += " ORDER BY json_extract(metadata, '$.name') ASC LIMIT ?"
    rows = conn.execute(base, (agent_id, limit)).fetchall()
    return [_row_to_skill(r) for r in rows]


def update_procedure(
    conn: sqlcipher3.Connection,
    *,
    external_id: str,
    procedure_md: str,
) -> bool:
    """Replace a skill's procedure body. Returns True when content changed.

    Bumps content_hash, resets embed_status to ``pending`` so the
    embed worker re-embeds with the new content, and lets the
    ``facets_au`` trigger refresh the FTS row. Returns False when the
    new procedure is byte-identical to the existing one (no update is
    written, no audit row).
    """

    skill = get_by_external_id(conn, external_id)
    if skill is None:
        raise UnknownSkillError(f"no skill with external_id {external_id!r}")
    new_hash = content_hash(procedure_md)
    if new_hash == skill.content_hash:
        return False
    try:
        conn.execute(
            """
            UPDATE facets
            SET content = ?, content_hash = ?, embed_status = 'pending',
                embed_attempts = 0, embed_last_error = NULL,
                embed_last_attempt_at = NULL
            WHERE external_id = ?
            """,
            (procedure_md, new_hash, external_id),
        )
    except (sqlite3.IntegrityError, sqlcipher3.IntegrityError) as exc:
        if "UNIQUE" in str(exc).upper():
            raise SkillContentNotUniqueError(
                f"another skill in this vault already carries the procedure body for skill {external_id!r}"
            ) from exc
        raise
    audit.write(
        conn,
        op="skill_procedure_updated",
        actor="system",
        agent_id=skill.agent_id,
        target_external_id=external_id,
        payload={"content_hash_prefix": new_hash[:12], "embed_status_reset": True},
    )
    # V0.5-P6 staleness wiring (ADR 0019 §Rationale 6). Skills are
    # a primary Playbook source; updating a skill's procedure
    # markdown changes what the compiler would synthesize, so any
    # Playbook citing this skill flips to is_stale=1.
    # ``update_metadata`` does NOT trigger this hook — name /
    # description / active toggles do not invalidate the compiled
    # narrative the way procedure body changes do.
    from tessera.vault import compiled

    compiled.mark_stale_for_source(
        conn,
        source_external_id=external_id,
        source_op="skill_procedure_updated",
        agent_id=skill.agent_id,
    )
    return True


def update_metadata(
    conn: sqlcipher3.Connection,
    *,
    external_id: str,
    name: str | None = None,
    description: str | None = None,
    active: bool | None = None,
) -> bool:
    """Edit one or more of ``name``, ``description``, ``active``.

    Returns True when at least one field actually changed; False when
    every supplied field already had the requested value (no UPDATE,
    no audit). Renaming a skill enforces the per-agent name-unique
    invariant by raising :class:`DuplicateSkillNameError` if the new
    name already exists on a live row.
    """

    skill = get_by_external_id(conn, external_id)
    if skill is None:
        raise UnknownSkillError(f"no skill with external_id {external_id!r}")
    metadata = dict(skill.metadata)
    changed: list[str] = []
    if name is not None:
        new_name = name.strip()
        if not new_name:
            raise SkillsError("name must be non-empty")
        if new_name != skill.name:
            collision = get_by_name(conn, agent_id=skill.agent_id, name=new_name)
            if collision is not None and collision.external_id != external_id:
                raise DuplicateSkillNameError(
                    f"skill named {new_name!r} already exists for agent {skill.agent_id}"
                )
            metadata["name"] = new_name
            changed.append("name")
    if description is not None and description != skill.description:
        metadata["description"] = description
        changed.append("description")
    if active is not None and active != skill.active:
        metadata["active"] = active
        changed.append("active")
    if not changed:
        return False
    conn.execute(
        "UPDATE facets SET metadata = ? WHERE external_id = ?",
        (json.dumps(metadata, sort_keys=True, ensure_ascii=False), external_id),
    )
    audit.write(
        conn,
        op="skill_metadata_updated",
        actor="system",
        agent_id=skill.agent_id,
        target_external_id=external_id,
        payload={"fields_changed": sorted(changed)},
    )
    return True


def set_disk_path(
    conn: sqlcipher3.Connection,
    *,
    external_id: str,
    disk_path: str | None,
) -> bool:
    """Assign or clear a skill's disk_path.

    Returns True when the column changed. Setting ``disk_path=None``
    clears the link (the file on disk is not deleted; that is a
    caller decision). Setting a path that is already used by another
    live skill raises :class:`DiskPathCollisionError` rather than
    letting the partial unique index fire as an IntegrityError.
    """

    skill = get_by_external_id(conn, external_id)
    if skill is None:
        raise UnknownSkillError(f"no skill with external_id {external_id!r}")
    if disk_path == skill.disk_path:
        return False
    if disk_path is not None:
        collision = conn.execute(
            """
            SELECT external_id FROM facets
            WHERE agent_id = ? AND disk_path = ? AND is_deleted = 0 AND external_id != ?
            """,
            (skill.agent_id, disk_path, external_id),
        ).fetchone()
        if collision is not None:
            raise DiskPathCollisionError(
                f"disk_path {disk_path!r} is already used by skill {collision[0]!r}"
            )
    conn.execute(
        "UPDATE facets SET disk_path = ? WHERE external_id = ?",
        (disk_path, external_id),
    )
    op = "skill_disk_path_set" if disk_path is not None else "skill_disk_path_cleared"
    audit.write(
        conn,
        op=op,
        actor="system",
        agent_id=skill.agent_id,
        target_external_id=external_id,
        payload={},
    )
    return True


def sync_to_disk(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    base_dir: Path,
) -> SyncToDiskReport:
    """Mirror every live, active skill to ``base_dir`` as ``{slug}.md``.

    Skills without a ``disk_path`` get one assigned (slugified name).
    Existing files whose contents match the in-vault procedure are
    skipped. Errors are collected per-file rather than aborting the
    whole sweep so one unwritable file does not block the others.
    """

    base_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    errors: list[str] = []
    paths: list[str] = []
    for skill in list_skills(conn, agent_id=agent_id, active_only=True, limit=10_000):
        try:
            target = _resolve_disk_path(conn, skill, base_dir)
            paths.append(str(target))
            if target.exists() and target.read_text(encoding="utf-8") == skill.procedure_md:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(skill.procedure_md, encoding="utf-8")
            written += 1
        except OSError as exc:
            errors.append(f"{skill.external_id}: {exc}")
    return SyncToDiskReport(
        written=written,
        skipped=skipped,
        errors=tuple(errors),
        paths=tuple(paths),
    )


def sync_from_disk(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    base_dir: Path,
    source_tool: str,
) -> SyncFromDiskReport:
    """Reconcile ``base_dir`` ``.md`` files into the skills surface.

    For each ``.md`` file, the path is matched against existing
    ``disk_path`` values: a hit updates the procedure (no-op when
    bytes match); a miss creates a new skill with name = file stem
    and description = "" so the user can fill it in via the MCP /
    CLI surface.
    """

    if not base_dir.exists():
        return SyncFromDiskReport()
    imported = 0
    updated = 0
    unchanged = 0
    errors: list[str] = []
    paths: list[str] = []
    for path in sorted(_walk_markdown(base_dir)):
        paths.append(str(path))
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        existing = _find_by_disk_path(conn, agent_id=agent_id, disk_path=str(path))
        if existing is not None:
            try:
                if update_procedure(conn, external_id=existing.external_id, procedure_md=body):
                    updated += 1
                else:
                    unchanged += 1
            except SkillsError as exc:
                errors.append(f"{path}: {exc}")
            continue
        try:
            external_id, _is_new = create_skill(
                conn,
                agent_id=agent_id,
                name=_name_from_stem(path.stem),
                description="",
                procedure_md=body,
                source_tool=source_tool,
            )
            set_disk_path(conn, external_id=external_id, disk_path=str(path))
            imported += 1
        except SkillsError as exc:
            errors.append(f"{path}: {exc}")
    return SyncFromDiskReport(
        imported=imported,
        updated=updated,
        unchanged=unchanged,
        errors=tuple(errors),
        paths=tuple(paths),
    )


def _resolve_disk_path(conn: sqlcipher3.Connection, skill: Skill, base_dir: Path) -> Path:
    if skill.disk_path is not None:
        return Path(skill.disk_path)
    candidate = base_dir / f"{slugify(skill.name)}.md"
    suffix = 2
    while True:
        if not _path_in_use(conn, agent_id=skill.agent_id, path=candidate):
            break
        candidate = base_dir / f"{slugify(skill.name)}-{suffix}.md"
        suffix += 1
    set_disk_path(conn, external_id=skill.external_id, disk_path=str(candidate))
    return candidate


def _path_in_use(conn: sqlcipher3.Connection, *, agent_id: int, path: Path) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM facets
        WHERE agent_id = ? AND disk_path = ? AND is_deleted = 0
        """,
        (agent_id, str(path)),
    ).fetchone()
    return row is not None


def _find_by_disk_path(
    conn: sqlcipher3.Connection, *, agent_id: int, disk_path: str
) -> Skill | None:
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, content_hash, captured_at,
               metadata, embed_status, disk_path, is_deleted
        FROM facets
        WHERE agent_id = ? AND facet_type = 'skill' AND is_deleted = 0
          AND disk_path = ?
        LIMIT 1
        """,
        (agent_id, disk_path),
    ).fetchone()
    if row is None:
        return None
    return _row_to_skill(row)


def _walk_markdown(base_dir: Path) -> Iterable[Path]:
    for path in base_dir.rglob("*.md"):
        if path.is_file():
            yield path


def _name_from_stem(stem: str) -> str:
    """Turn a slug-style filename stem into a default skill name.

    The slug → name conversion is a best-effort pretty-print —
    ``git-rebase`` becomes ``git rebase``, but the user is expected to
    edit the metadata to a more descriptive label after import. We
    keep punctuation simple here so the import path stays predictable.
    """

    return " ".join(part for part in stem.replace("_", "-").split("-") if part) or stem


def _row_to_skill(row: tuple[Any, ...]) -> Skill:
    metadata = json.loads(row[6]) if row[6] else {}
    return Skill(
        facet_id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        name=str(metadata.get("name", "")),
        description=str(metadata.get("description", "")),
        active=bool(metadata.get("active", True)),
        procedure_md=str(row[3]),
        content_hash=str(row[4]),
        disk_path=str(row[8]) if row[8] is not None else None,
        captured_at=int(row[5]),
        embed_status=str(row[7]),
        metadata=metadata,
    )
