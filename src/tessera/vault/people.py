"""CRUD over the ``people`` and ``person_mentions`` tables.

The People surface (v0.3) gives every facet vault a first-class model
of the people the user works with. ``people`` rows are scoped per
agent, carry a stable ``external_id`` ULID, a canonical name, and a
JSON array of aliases; ``person_mentions`` rows link facets to the
people they reference with a confidence score.

This module owns:

* Insert with canonical-name dedup per agent.
* Alias maintenance (add/remove) on existing rows.
* Merge: collapse two ``people`` rows into one, migrating aliases and
  every ``person_mentions`` row across to the survivor.
* Split: extract one alias into a new ``people`` row, migrating the
  mentions that were attached via that alias if the caller specifies.
* Resolve: turn a free-form mention string into either a single match
  or a candidate list the caller must disambiguate before linking.

Resolution is deliberately conservative — the v0.3 spec calls for
"explicit confirmation for fuzzy matches" so the resolver returns
candidates rather than auto-picking. Fuzzy matching at this stage is
case-insensitive prefix and substring matching against canonical name
+ aliases; Levenshtein-style scoring lands later if real-user data
shows the conservative path mis-classifies.

Audit emission for every mutation is per ``vault.audit`` allowlist;
canonical names and aliases never enter payloads (§S4 — no user
content in audit rows).
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlcipher3
from ulid import ULID

from tessera.vault import audit


class PeopleError(Exception):
    """Base class for people-module failures."""


class UnknownPersonError(PeopleError):
    """Referenced person external_id does not exist."""


class UnknownFacetError(PeopleError):
    """Referenced facet external_id does not exist."""


class DuplicateCanonicalNameError(PeopleError):
    """A person with the same canonical name already exists for this agent."""


class AmbiguousMentionError(PeopleError):
    """Resolution returned multiple candidates and the caller did not
    pre-pick a disambiguation external_id."""


@dataclass(frozen=True, slots=True)
class Person:
    id: int
    external_id: str
    agent_id: int
    canonical_name: str
    aliases: tuple[str, ...]
    metadata: dict[str, Any]
    created_at: int


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Outcome of a free-form-mention lookup.

    ``matches`` carries every candidate; downstream code reads
    ``len(matches)`` and ``is_exact`` to decide whether to auto-link or
    surface the list to the user. An empty list means no candidate
    matched at all — the caller can then offer to create a new person.
    """

    matches: tuple[Person, ...]
    is_exact: bool


def _normalize(name: str) -> str:
    """Trim and NFC-normalize a name without lowercasing.

    Resolution does case-insensitive comparison via SQL LOWER() so we
    keep canonical case in storage. Whitespace is collapsed and the
    string is NFC-normalized so equivalent unicode forms compare equal
    at the application layer the same way ``facets.content_hash`` does.
    """

    return unicodedata.normalize("NFC", " ".join(name.split()))


def insert(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    canonical_name: str,
    aliases: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: int | None = None,
) -> tuple[str, bool]:
    """Insert a person, deduplicating on ``(agent_id, canonical_name)``.

    Returns ``(external_id, is_new)``. When a person with the same
    normalized canonical name already exists for this agent, the
    existing row is returned with ``is_new=False`` and any *new* aliases
    in the ``aliases`` argument are merged into the existing alias list.
    """

    canonical = _normalize(canonical_name)
    if not canonical:
        raise PeopleError("canonical_name must be non-empty after normalization")

    existing = conn.execute(
        "SELECT external_id FROM people WHERE agent_id = ? AND canonical_name = ?",
        (agent_id, canonical),
    ).fetchone()
    if existing is not None:
        existing_id = str(existing[0])
        if aliases:
            for alias in aliases:
                _add_alias_inner(conn, existing_id, alias, audit_emit=False)
        return existing_id, False

    external_id = str(ULID())
    created = created_at if created_at is not None else _now_epoch()
    normalized_aliases = sorted({_normalize(a) for a in (aliases or []) if _normalize(a)})
    aliases_json = json.dumps(normalized_aliases, ensure_ascii=False)
    meta_json = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)
    try:
        conn.execute(
            """
            INSERT INTO people(external_id, agent_id, canonical_name, aliases, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (external_id, agent_id, canonical, aliases_json, meta_json, created),
        )
    except (sqlite3.IntegrityError, sqlcipher3.IntegrityError) as exc:
        if "FOREIGN KEY" in str(exc).upper():
            raise PeopleError(f"no agent with id {agent_id}") from exc
        if "UNIQUE" in str(exc).upper():
            raise DuplicateCanonicalNameError(
                f"person with canonical_name {canonical!r} already exists for agent {agent_id}"
            ) from exc
        raise

    audit.write(
        conn,
        op="person_created",
        actor="system",
        agent_id=agent_id,
        target_external_id=external_id,
        payload={"alias_count": len(normalized_aliases)},
    )
    return external_id, True


def get(conn: sqlcipher3.Connection, external_id: str) -> Person | None:
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
        FROM people WHERE external_id = ?
        """,
        (external_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_person(row)


def get_by_canonical_name(
    conn: sqlcipher3.Connection, *, agent_id: int, canonical_name: str
) -> Person | None:
    canonical = _normalize(canonical_name)
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
        FROM people WHERE agent_id = ? AND canonical_name = ?
        """,
        (agent_id, canonical),
    ).fetchone()
    if row is None:
        return None
    return _row_to_person(row)


def list_by_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    limit: int = 50,
    since: int | None = None,
) -> list[Person]:
    if since is None:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
            FROM people WHERE agent_id = ?
            ORDER BY canonical_name ASC
            LIMIT ?
            """,
            (agent_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
            FROM people WHERE agent_id = ? AND created_at >= ?
            ORDER BY canonical_name ASC
            LIMIT ?
            """,
            (agent_id, since, limit),
        ).fetchall()
    return [_row_to_person(r) for r in rows]


def add_alias(conn: sqlcipher3.Connection, *, external_id: str, alias: str) -> bool:
    """Append ``alias`` to a person's alias list. Returns True on append.

    The list is kept sorted and deduplicated on the normalized form, so
    re-adding an existing alias is a no-op (returns False without
    writing audit).
    """

    return _add_alias_inner(conn, external_id, alias, audit_emit=True)


def _add_alias_inner(
    conn: sqlcipher3.Connection, external_id: str, alias: str, *, audit_emit: bool
) -> bool:
    normalized = _normalize(alias)
    if not normalized:
        return False
    row = conn.execute(
        "SELECT id, agent_id, aliases FROM people WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    if row is None:
        raise UnknownPersonError(f"no person with external_id {external_id!r}")
    aliases = sorted(set(json.loads(row[2])) | {normalized})
    before_count = len(json.loads(row[2]))
    if len(aliases) == before_count:
        return False
    conn.execute(
        "UPDATE people SET aliases = ? WHERE external_id = ?",
        (json.dumps(aliases, ensure_ascii=False), external_id),
    )
    if audit_emit:
        audit.write(
            conn,
            op="person_alias_added",
            actor="system",
            agent_id=int(row[1]),
            target_external_id=external_id,
            payload={"alias_count_after": len(aliases)},
        )
    return True


def merge(
    conn: sqlcipher3.Connection,
    *,
    primary_external_id: str,
    secondary_external_id: str,
) -> Person:
    """Collapse ``secondary`` into ``primary``.

    Migrates every alias and every ``person_mentions`` row from
    ``secondary`` to ``primary``, drops the secondary row, and writes a
    single ``person_merged`` audit entry on the survivor. Mentions that
    would create a duplicate ``(facet_id, primary_id)`` pair are
    skipped (the survivor's existing link wins) so the UNIQUE
    constraint never fires.
    """

    if primary_external_id == secondary_external_id:
        raise PeopleError("cannot merge a person into itself")
    primary = get(conn, primary_external_id)
    if primary is None:
        raise UnknownPersonError(f"no person with external_id {primary_external_id!r}")
    secondary = get(conn, secondary_external_id)
    if secondary is None:
        raise UnknownPersonError(f"no person with external_id {secondary_external_id!r}")
    if primary.agent_id != secondary.agent_id:
        raise PeopleError("cannot merge people across agent boundaries")

    merged_aliases = sorted(
        set(primary.aliases) | set(secondary.aliases) | {secondary.canonical_name}
    )
    aliases_added = len(merged_aliases) - len(primary.aliases)
    conn.execute(
        "UPDATE people SET aliases = ? WHERE id = ?",
        (json.dumps(merged_aliases, ensure_ascii=False), primary.id),
    )
    cur = conn.execute(
        """
        UPDATE OR IGNORE person_mentions SET person_id = ? WHERE person_id = ?
        """,
        (primary.id, secondary.id),
    )
    mentions_migrated = int(cur.rowcount)
    # Any rows that lost the OR IGNORE race (because the primary already
    # had that facet linked) survive on the secondary; drop them so the
    # secondary is fully drained.
    conn.execute("DELETE FROM person_mentions WHERE person_id = ?", (secondary.id,))
    conn.execute("DELETE FROM people WHERE id = ?", (secondary.id,))
    audit.write(
        conn,
        op="person_merged",
        actor="system",
        agent_id=primary.agent_id,
        target_external_id=primary_external_id,
        payload={
            "secondary_external_id": secondary_external_id,
            "mentions_migrated": mentions_migrated,
            "aliases_migrated": aliases_added,
        },
    )
    refreshed = get(conn, primary_external_id)
    if refreshed is None:
        raise PeopleError("merge result vanished mid-transaction")
    return refreshed


def split(
    conn: sqlcipher3.Connection,
    *,
    person_external_id: str,
    extracted_canonical_name: str,
    move_aliases: list[str] | None = None,
) -> tuple[Person, Person]:
    """Extract a new ``people`` row out of an existing one.

    The new person inherits the supplied canonical name (which must not
    already exist for the agent) and any aliases listed in
    ``move_aliases`` (which are removed from the original). No mentions
    are reassigned automatically — callers re-link mentions explicitly
    after a split because the choice of which mentions belong to which
    person is a content judgement that lives outside this layer.
    """

    original = get(conn, person_external_id)
    if original is None:
        raise UnknownPersonError(f"no person with external_id {person_external_id!r}")
    new_canonical = _normalize(extracted_canonical_name)
    if not new_canonical:
        raise PeopleError("extracted_canonical_name must be non-empty after normalization")
    if new_canonical == original.canonical_name:
        raise PeopleError("extracted_canonical_name must differ from the original")

    move_set = {_normalize(a) for a in (move_aliases or []) if _normalize(a)}
    remaining = sorted(set(original.aliases) - move_set)
    new_external_id, is_new = insert(
        conn,
        agent_id=original.agent_id,
        canonical_name=new_canonical,
        aliases=sorted(move_set),
    )
    if not is_new:
        raise DuplicateCanonicalNameError(
            f"cannot split: a person with canonical_name {new_canonical!r} already exists"
        )
    conn.execute(
        "UPDATE people SET aliases = ? WHERE id = ?",
        (json.dumps(remaining, ensure_ascii=False), original.id),
    )
    audit.write(
        conn,
        op="person_split",
        actor="system",
        agent_id=original.agent_id,
        target_external_id=person_external_id,
        payload={"new_external_id": new_external_id},
    )
    new_person = get(conn, new_external_id)
    refreshed_original = get(conn, person_external_id)
    if new_person is None or refreshed_original is None:
        raise PeopleError("split result vanished mid-transaction")
    return refreshed_original, new_person


def resolve(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    mention: str,
) -> ResolveResult:
    """Map a free-form mention string to candidate ``Person`` rows.

    Resolution order:

    1. Exact canonical-name match (case-insensitive after NFC). One hit
       returns ``is_exact=True``.
    2. Exact alias match. One hit returns ``is_exact=True``.
    3. Prefix match against canonical names and aliases. Returns every
       hit with ``is_exact=False`` so the caller disambiguates.

    A query that returns nothing yields an empty match list.
    """

    needle = _normalize(mention)
    if not needle:
        return ResolveResult(matches=(), is_exact=False)

    rows = conn.execute(
        """
        SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
        FROM people
        WHERE agent_id = ? AND LOWER(canonical_name) = LOWER(?)
        """,
        (agent_id, needle),
    ).fetchall()
    if rows:
        return ResolveResult(
            matches=tuple(_row_to_person(r) for r in rows), is_exact=len(rows) == 1
        )

    # Alias match: scan rows whose alias JSON contains a case-insensitive
    # match. SQLite does not have a JSON-array case-insensitive contains,
    # so we filter in Python after a coarse LIKE on the JSON literal.
    candidate_rows = conn.execute(
        """
        SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
        FROM people
        WHERE agent_id = ? AND aliases LIKE ?
        """,
        (agent_id, f"%{needle}%"),
    ).fetchall()
    needle_low = needle.lower()
    alias_hits = [
        _row_to_person(r)
        for r in candidate_rows
        if any(needle_low == a.lower() for a in json.loads(r[4]))
    ]
    if alias_hits:
        return ResolveResult(matches=tuple(alias_hits), is_exact=len(alias_hits) == 1)

    # Prefix / substring fallback against canonical and alias.
    prefix_rows = conn.execute(
        """
        SELECT id, external_id, agent_id, canonical_name, aliases, metadata, created_at
        FROM people
        WHERE agent_id = ?
          AND (LOWER(canonical_name) LIKE LOWER(?) OR aliases LIKE ?)
        ORDER BY canonical_name ASC
        """,
        (agent_id, f"{needle}%", f"%{needle}%"),
    ).fetchall()
    prefix_hits = []
    for r in prefix_rows:
        canonical = str(r[3])
        aliases = [str(a) for a in json.loads(r[4])]
        if canonical.lower().startswith(needle_low) or any(
            needle_low in a.lower() for a in aliases
        ):
            prefix_hits.append(_row_to_person(r))
    return ResolveResult(matches=tuple(prefix_hits), is_exact=False)


def link_facet_mention(
    conn: sqlcipher3.Connection,
    *,
    facet_external_id: str,
    person_external_id: str,
    confidence: float = 1.0,
) -> bool:
    """Link a facet to a person via ``person_mentions``.

    Returns True when a new link is written, False when the link
    already existed (the existing confidence is left untouched). A
    higher-confidence relink is an explicit ``unlink`` + ``link`` —
    silently overwriting the score would erase the audit trail of the
    earlier judgement.
    """

    if not 0.0 <= confidence <= 1.0:
        raise PeopleError(f"confidence {confidence!r} outside [0.0, 1.0]")
    facet_row = conn.execute(
        "SELECT id FROM facets WHERE external_id = ?", (facet_external_id,)
    ).fetchone()
    if facet_row is None:
        raise UnknownFacetError(f"no facet with external_id {facet_external_id!r}")
    person_row = conn.execute(
        "SELECT id, agent_id FROM people WHERE external_id = ?", (person_external_id,)
    ).fetchone()
    if person_row is None:
        raise UnknownPersonError(f"no person with external_id {person_external_id!r}")
    facet_id = int(facet_row[0])
    person_id = int(person_row[0])
    existing = conn.execute(
        "SELECT 1 FROM person_mentions WHERE facet_id = ? AND person_id = ?",
        (facet_id, person_id),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO person_mentions(facet_id, person_id, confidence) VALUES (?, ?, ?)",
        (facet_id, person_id, confidence),
    )
    audit.write(
        conn,
        op="person_mention_linked",
        actor="system",
        agent_id=int(person_row[1]),
        target_external_id=facet_external_id,
        payload={"person_external_id": person_external_id, "confidence": confidence},
    )
    return True


def unlink_facet_mention(
    conn: sqlcipher3.Connection,
    *,
    facet_external_id: str,
    person_external_id: str,
) -> bool:
    """Drop the ``(facet, person)`` link if present. Returns True on delete."""

    facet_row = conn.execute(
        "SELECT id FROM facets WHERE external_id = ?", (facet_external_id,)
    ).fetchone()
    if facet_row is None:
        raise UnknownFacetError(f"no facet with external_id {facet_external_id!r}")
    person_row = conn.execute(
        "SELECT id, agent_id FROM people WHERE external_id = ?", (person_external_id,)
    ).fetchone()
    if person_row is None:
        raise UnknownPersonError(f"no person with external_id {person_external_id!r}")
    cur = conn.execute(
        "DELETE FROM person_mentions WHERE facet_id = ? AND person_id = ?",
        (int(facet_row[0]), int(person_row[0])),
    )
    if int(cur.rowcount) == 0:
        return False
    audit.write(
        conn,
        op="person_mention_unlinked",
        actor="system",
        agent_id=int(person_row[1]),
        target_external_id=facet_external_id,
        payload={"person_external_id": person_external_id},
    )
    return True


def people_for_facet(
    conn: sqlcipher3.Connection, *, facet_external_id: str
) -> list[tuple[Person, float]]:
    """Return every ``(person, confidence)`` linked to ``facet_external_id``."""

    rows = conn.execute(
        """
        SELECT p.id, p.external_id, p.agent_id, p.canonical_name, p.aliases,
               p.metadata, p.created_at, pm.confidence
        FROM person_mentions pm
        JOIN people p ON p.id = pm.person_id
        JOIN facets f ON f.id = pm.facet_id
        WHERE f.external_id = ?
        ORDER BY pm.confidence DESC, p.canonical_name ASC
        """,
        (facet_external_id,),
    ).fetchall()
    return [(_row_to_person(r[:7]), float(r[7])) for r in rows]


def facets_for_person(
    conn: sqlcipher3.Connection,
    *,
    person_external_id: str,
    limit: int = 50,
) -> list[tuple[str, float]]:
    """Return every ``(facet_external_id, confidence)`` linked to a person.

    Only live (non-soft-deleted) facets are returned. Ordered by
    confidence descending, then captured_at descending so the highest-
    signal recent mentions surface first.
    """

    rows = conn.execute(
        """
        SELECT f.external_id, pm.confidence
        FROM person_mentions pm
        JOIN people p ON p.id = pm.person_id
        JOIN facets f ON f.id = pm.facet_id
        WHERE p.external_id = ? AND f.is_deleted = 0
        ORDER BY pm.confidence DESC, f.captured_at DESC
        LIMIT ?
        """,
        (person_external_id, limit),
    ).fetchall()
    return [(str(r[0]), float(r[1])) for r in rows]


def _row_to_person(row: tuple[Any, ...]) -> Person:
    return Person(
        id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        canonical_name=str(row[3]),
        aliases=tuple(json.loads(row[4])) if row[4] else (),
        metadata=json.loads(row[5]) if row[5] else {},
        created_at=int(row[6]),
    )


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
