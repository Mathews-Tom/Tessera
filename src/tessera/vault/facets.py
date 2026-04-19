"""CRUD over the ``facets`` table.

This module owns content-hash deduplication, soft/hard delete semantics, and
the read helpers the MCP surface will call in later phases. ``hard_delete``
cascades across every registered ``vec_<id>`` virtual table in one
transaction so erasure actually erases (``docs/threat-model.md §S7``). FTS
rows are maintained by the ``facets_ad`` / ``facets_au`` triggers installed
with the schema.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlcipher3
from ulid import ULID

V0_1_FACET_TYPES: Final[frozenset[str]] = frozenset({"episodic", "semantic", "style"})


class FacetError(Exception):
    """Base class for facets-module failures."""


class UnsupportedFacetTypeError(FacetError):
    """Facet type is outside the v0.1 supported set."""


class UnknownAgentError(FacetError):
    """Referenced agent_id does not exist in ``agents``."""


@dataclass(frozen=True, slots=True)
class Facet:
    id: int
    external_id: str
    agent_id: int
    facet_type: str
    content: str
    content_hash: str
    source_client: str
    captured_at: int
    metadata: dict[str, Any]
    is_deleted: bool
    embed_status: str


def content_hash(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def insert(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_type: str,
    content: str,
    source_client: str,
    metadata: dict[str, Any] | None = None,
    captured_at: int | None = None,
) -> tuple[str, bool]:
    """Insert a facet, deduplicating on ``(agent_id, content_hash)``.

    Returns ``(external_id, is_new)``. When a facet with the same normalized
    content already exists for this agent, the existing ``external_id`` is
    returned with ``is_new=False`` and no row is written.
    """

    if facet_type not in V0_1_FACET_TYPES:
        raise UnsupportedFacetTypeError(
            f"facet_type {facet_type!r} not supported at v0.1; expected one of {sorted(V0_1_FACET_TYPES)}"
        )
    digest = content_hash(content)
    # Dedup sees live AND soft-deleted rows because the UNIQUE(agent_id,
    # content_hash) constraint covers both. A live hit returns the existing
    # id with is_new=False; a soft-deleted hit restores the row (clears
    # is_deleted / deleted_at) so re-capturing content the user previously
    # removed is treated as an intentional un-delete rather than a silent
    # collision with tombstone metadata.
    existing = conn.execute(
        "SELECT external_id, is_deleted FROM facets WHERE agent_id = ? AND content_hash = ?",
        (agent_id, digest),
    ).fetchone()
    if existing is not None:
        existing_id = str(existing[0])
        was_deleted = bool(existing[1])
        if was_deleted:
            conn.execute(
                "UPDATE facets SET is_deleted = 0, deleted_at = NULL WHERE external_id = ?",
                (existing_id,),
            )
        return existing_id, False

    external_id = str(ULID())
    captured = captured_at if captured_at is not None else _now_epoch()
    meta_json = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)
    try:
        conn.execute(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                source_client, captured_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                agent_id,
                facet_type,
                content,
                digest,
                source_client,
                captured,
                meta_json,
            ),
        )
    except (sqlite3.IntegrityError, sqlcipher3.IntegrityError) as exc:
        if "FOREIGN KEY" in str(exc).upper():
            raise UnknownAgentError(f"no agent with id {agent_id}") from exc
        raise
    return external_id, True


def get(conn: sqlcipher3.Connection, external_id: str) -> Facet | None:
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, facet_type, content, content_hash,
               source_client, captured_at, metadata, is_deleted, embed_status
        FROM facets WHERE external_id = ?
        """,
        (external_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_facet(row)


def list_by_type(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_type: str,
    limit: int = 10,
    since: int | None = None,
) -> list[Facet]:
    if facet_type not in V0_1_FACET_TYPES:
        raise UnsupportedFacetTypeError(f"facet_type {facet_type!r} not supported at v0.1")
    if since is None:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, facet_type, content, content_hash,
                   source_client, captured_at, metadata, is_deleted, embed_status
            FROM facets
            WHERE agent_id = ? AND facet_type = ? AND is_deleted = 0
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (agent_id, facet_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, facet_type, content, content_hash,
                   source_client, captured_at, metadata, is_deleted, embed_status
            FROM facets
            WHERE agent_id = ? AND facet_type = ? AND is_deleted = 0
              AND captured_at >= ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (agent_id, facet_type, since, limit),
        ).fetchall()
    return [_row_to_facet(r) for r in rows]


def soft_delete(conn: sqlcipher3.Connection, external_id: str) -> bool:
    cur = conn.execute(
        "UPDATE facets SET is_deleted = 1, deleted_at = ? WHERE external_id = ? AND is_deleted = 0",
        (_now_epoch(), external_id),
    )
    rowcount: int = cur.rowcount
    return rowcount == 1


def hard_delete(conn: sqlcipher3.Connection, external_id: str) -> bool:
    """Remove the facet row and every associated vector and FTS entry.

    FTS cascade happens automatically via the ``facets_ad`` trigger. Vec
    tables are sqlite-vec virtual tables and do not support triggers, so
    the cascade across every registered ``vec_<id>`` runs as an explicit
    set of DELETEs before the facet row is removed. All writes run inside
    one transaction so a crash cannot leave orphan vector rows referencing
    a deleted facet (`docs/threat-model.md §S7` — erasure must actually
    erase).
    """

    row = conn.execute("SELECT id FROM facets WHERE external_id = ?", (external_id,)).fetchone()
    if row is None:
        return False
    facet_id = int(row[0])
    model_rows = conn.execute("SELECT id FROM embedding_models").fetchall()
    # Loading sqlite-vec once is enough: the extension stays attached to the
    # connection for the remainder of its lifetime. Importing models_registry
    # would flip the dependency direction; the ensure-loaded probe lives
    # inline here to keep vault/ free of adapter-layer imports.
    if model_rows:
        _ensure_vec_loaded(conn)
    # Savepoint (not BEGIN) so the caller can already be inside a
    # transaction — for example the sqlite3 test connection that runs in
    # pysqlite's legacy auto-begin mode. Production VaultConnection is
    # autocommit, so the savepoint becomes the only transaction scope for
    # the cascade.
    conn.execute("SAVEPOINT hard_delete")
    try:
        for model_row in model_rows:
            model_id = int(model_row[0])
            conn.execute(f"DELETE FROM vec_{model_id} WHERE facet_id = ?", (facet_id,))
        cur = conn.execute("DELETE FROM facets WHERE id = ?", (facet_id,))
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT hard_delete")
        conn.execute("RELEASE SAVEPOINT hard_delete")
        raise
    conn.execute("RELEASE SAVEPOINT hard_delete")
    rowcount: int = cur.rowcount
    return rowcount == 1


def _ensure_vec_loaded(conn: sqlcipher3.Connection) -> None:
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return
    except (sqlcipher3.OperationalError, sqlcipher3.DatabaseError):
        pass
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _row_to_facet(row: tuple[Any, ...]) -> Facet:
    return Facet(
        id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        facet_type=str(row[3]),
        content=str(row[4]),
        content_hash=str(row[5]),
        source_client=str(row[6]),
        captured_at=int(row[7]),
        metadata=json.loads(row[8]) if row[8] else {},
        is_deleted=bool(row[9]),
        embed_status=str(row[10]),
    )


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
