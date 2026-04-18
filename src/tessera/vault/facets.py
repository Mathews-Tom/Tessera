"""CRUD over the ``facets`` table.

This module owns content-hash deduplication, soft/hard delete semantics, and
the read helpers the MCP surface will call in later phases. Embedding-vector
cleanup is scoped out — the per-model ``vec_*`` virtual tables do not exist
until P2, so ``hard_delete`` calls a no-op vec cascade and documents the
follow-up in docs/migration-contract.md terms. FTS rows are maintained by
the ``facets_ad`` / ``facets_au`` triggers installed with the schema.
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
    existing = conn.execute(
        "SELECT external_id FROM facets WHERE agent_id = ? AND content_hash = ?",
        (agent_id, digest),
    ).fetchone()
    if existing is not None:
        return str(existing[0]), False

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
    """Remove the facet row.

    FTS cascade happens automatically via the ``facets_ad`` trigger. The
    per-model ``vec_*`` cleanup is a no-op at P1 because no vec table has
    been created yet; that cascade lands with the capture-and-embed work
    in P3.
    """

    cur = conn.execute("DELETE FROM facets WHERE external_id = ?", (external_id,))
    rowcount: int = cur.rowcount
    return rowcount == 1


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
