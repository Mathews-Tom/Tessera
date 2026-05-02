"""CRUD over the ``facets`` table.

This module owns content-hash deduplication, soft/hard delete semantics, and
the read helpers the MCP surface calls. ``hard_delete`` cascades across every
registered ``vec_<id>`` virtual table in one transaction so erasure actually
erases (``docs/threat-model.md §S7``). FTS rows are maintained by the
``facets_ai`` / ``facets_ad`` / ``facets_au`` triggers installed with the
schema.

The facet-type allowlist is post-reframe (ADR 0010). v0.3 unlocks
``person`` and ``skill`` for writes alongside the original five v0.1
types; ``compiled_notebook`` remains reserved in the schema CHECK but
unwritable until v0.5 activates it.
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

from tessera.vault.connection import ensure_vec_loaded, savepoint

# v0.1 writable facet types (ADR 0010). Retained as a named subset so
# importers and pre-v0.3 fixtures can still target the original
# vocabulary explicitly — the active write-path allowlist is
# ``WRITABLE_FACET_TYPES`` below.
V0_1_FACET_TYPES: Final[frozenset[str]] = frozenset(
    {"identity", "preference", "workflow", "project", "style"}
)

# v0.3 unlocks ``person`` and ``skill`` for writes; v0.5 unlocks
# ``agent_profile`` (V0.5-P2 / ADR 0017) alongside the Phase-3
# ``verification_checklist`` + ``retrospective`` and Phase-5
# ``automation`` reservations. The forward-compatibility allowlists
# mirror the schema CHECK so each sub-phase activates a new write path
# by swapping the allowlist the capture surface consults rather than
# editing scattered literals.
V0_3_FACET_TYPES: Final[frozenset[str]] = V0_1_FACET_TYPES | frozenset({"person", "skill"})
V0_5_RESERVED_FACET_TYPES: Final[frozenset[str]] = frozenset(
    {
        "compiled_notebook",
        "agent_profile",
        "verification_checklist",
        "retrospective",
        "automation",
    }
)
V0_5_FACET_TYPES: Final[frozenset[str]] = V0_3_FACET_TYPES | V0_5_RESERVED_FACET_TYPES

# The facet-type set the active write path accepts. V0.5-P2 activates
# ``agent_profile`` — Tessera registers agent profiles as recallable
# context per ADR 0017. The remaining v0.5 reserved types stay
# CHECK-permitted but write-rejected until their sub-phases ship.
WRITABLE_FACET_TYPES: Final[frozenset[str]] = V0_3_FACET_TYPES | frozenset({"agent_profile"})

# Superset of every facet type the schema CHECK permits. Used by the scope
# layer — a token may be scoped for read against a reserved type even when
# the write path rejects that type today.
ALL_FACET_TYPES: Final[frozenset[str]] = V0_5_FACET_TYPES

# v0.5-P1 memory volatility (ADR 0016). The CHECK constraint on
# ``facets.volatility`` mirrors this set; capture writes default to
# ``persistent``; SWCR weights ``session``/``ephemeral`` rows by the
# closed-form ``freshness(f)`` term.
WRITABLE_VOLATILITIES: Final[frozenset[str]] = frozenset({"persistent", "session", "ephemeral"})

# Default TTLs per volatility. ADR 0016 fixes ``session=24h`` and
# ``ephemeral=60min``; ``persistent`` rows have no TTL. Callers may
# override per row with ``ttl_seconds`` up to the per-volatility ceiling.
_SECONDS_PER_HOUR: Final[int] = 3600
DEFAULT_TTL_SECONDS: Final[dict[str, int | None]] = {
    "persistent": None,
    "session": 24 * _SECONDS_PER_HOUR,
    "ephemeral": 60 * 60,
}
MAX_TTL_SECONDS: Final[dict[str, int | None]] = {
    "persistent": None,
    "session": 7 * 24 * _SECONDS_PER_HOUR,  # one week ceiling on session rows
    "ephemeral": 24 * _SECONDS_PER_HOUR,  # ADR 0016: ephemeral max 24h
}


class FacetError(Exception):
    """Base class for facets-module failures."""


class UnsupportedFacetTypeError(FacetError):
    """Facet type is outside the v0.1 supported set."""


class UnsupportedVolatilityError(FacetError):
    """Volatility value is outside the ADR-0016 set."""


class InvalidTTLError(FacetError):
    """TTL is not consistent with the row's volatility."""


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
    source_tool: str
    captured_at: int
    metadata: dict[str, Any]
    is_deleted: bool
    embed_status: str
    volatility: str = "persistent"
    ttl_seconds: int | None = None


def resolve_ttl_seconds(volatility: str, ttl_seconds: int | None) -> int | None:
    """Pick the effective TTL for a row given its volatility and override.

    Persistent rows force ``ttl_seconds=None`` regardless of the override.
    Non-persistent rows take the override when provided and inside the
    per-volatility ceiling, else the volatility's default.
    """

    if volatility not in WRITABLE_VOLATILITIES:
        raise UnsupportedVolatilityError(
            f"volatility {volatility!r} not in {sorted(WRITABLE_VOLATILITIES)}"
        )
    if volatility == "persistent":
        if ttl_seconds is not None:
            raise InvalidTTLError("persistent rows cannot carry a TTL")
        return None
    ceiling = MAX_TTL_SECONDS[volatility]
    if ttl_seconds is None:
        return DEFAULT_TTL_SECONDS[volatility]
    if ttl_seconds <= 0:
        raise InvalidTTLError(f"ttl_seconds must be positive; got {ttl_seconds}")
    if ceiling is not None and ttl_seconds > ceiling:
        raise InvalidTTLError(
            f"ttl_seconds={ttl_seconds} exceeds {volatility} ceiling of {ceiling}s"
        )
    return ttl_seconds


def content_hash(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def insert(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_type: str,
    content: str,
    source_tool: str,
    metadata: dict[str, Any] | None = None,
    captured_at: int | None = None,
    volatility: str = "persistent",
    ttl_seconds: int | None = None,
) -> tuple[str, bool]:
    """Insert a facet, deduplicating on ``(agent_id, content_hash)``.

    Returns ``(external_id, is_new)``. When a facet with the same normalized
    content already exists for this agent, the existing ``external_id`` is
    returned with ``is_new=False`` and no row is written.
    """

    if facet_type not in WRITABLE_FACET_TYPES:
        raise UnsupportedFacetTypeError(
            f"facet_type {facet_type!r} not writable; expected one of {sorted(WRITABLE_FACET_TYPES)}"
        )
    effective_ttl = resolve_ttl_seconds(volatility, ttl_seconds)
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
                source_tool, captured_at, metadata, volatility, ttl_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                agent_id,
                facet_type,
                content,
                digest,
                source_tool,
                captured,
                meta_json,
                volatility,
                effective_ttl,
            ),
        )
    except (sqlite3.IntegrityError, sqlcipher3.IntegrityError) as exc:
        if "FOREIGN KEY" in str(exc).upper():
            raise UnknownAgentError(f"no agent with id {agent_id}") from exc
        raise
    return external_id, True


_FACET_SELECT_COLS: Final[str] = (
    "id, external_id, agent_id, facet_type, content, content_hash, "
    "source_tool, captured_at, metadata, is_deleted, embed_status, "
    "volatility, ttl_seconds"
)


def get(conn: sqlcipher3.Connection, external_id: str) -> Facet | None:
    row = conn.execute(
        f"SELECT {_FACET_SELECT_COLS} FROM facets WHERE external_id = ?",
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
    if facet_type not in WRITABLE_FACET_TYPES:
        raise UnsupportedFacetTypeError(f"facet_type {facet_type!r} not writable")
    if since is None:
        rows = conn.execute(
            f"""
            SELECT {_FACET_SELECT_COLS}
            FROM facets
            WHERE agent_id = ? AND facet_type = ? AND is_deleted = 0
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (agent_id, facet_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_FACET_SELECT_COLS}
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
    return int(cur.rowcount) == 1


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
    if model_rows:
        ensure_vec_loaded(conn)
    with savepoint(conn, "hard_delete"):
        for model_row in model_rows:
            model_id = int(model_row[0])
            conn.execute(f"DELETE FROM vec_{model_id} WHERE facet_id = ?", (facet_id,))
        cur = conn.execute("DELETE FROM facets WHERE id = ?", (facet_id,))
    return int(cur.rowcount) == 1


def _row_to_facet(row: tuple[Any, ...]) -> Facet:
    return Facet(
        id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        facet_type=str(row[3]),
        content=str(row[4]),
        content_hash=str(row[5]),
        source_tool=str(row[6]),
        captured_at=int(row[7]),
        metadata=json.loads(row[8]) if row[8] else {},
        is_deleted=bool(row[9]),
        embed_status=str(row[10]),
        volatility=str(row[11]) if row[11] is not None else "persistent",
        ttl_seconds=int(row[12]) if row[12] is not None else None,
    )


def list_expired_volatile(
    conn: sqlcipher3.Connection,
    *,
    now: int,
    limit: int = 256,
) -> list[Facet]:
    """Return non-persistent rows whose TTL has elapsed.

    Used by the auto-compaction sweep. Rows missing ``ttl_seconds`` (a
    schema-v3 row migrated to v4 with a non-default volatility but no TTL
    yet) fall back to the volatility's default TTL so the sweep cannot
    miss them. Persistent rows are filtered out by the partial index.
    """

    rows = conn.execute(
        f"""
        SELECT {_FACET_SELECT_COLS}
        FROM facets
        WHERE is_deleted = 0
          AND volatility IN ('session', 'ephemeral')
          AND captured_at + COALESCE(
                ttl_seconds,
                CASE volatility
                    WHEN 'session' THEN ?
                    WHEN 'ephemeral' THEN ?
                END
            ) <= ?
        ORDER BY captured_at ASC
        LIMIT ?
        """,
        (
            DEFAULT_TTL_SECONDS["session"],
            DEFAULT_TTL_SECONDS["ephemeral"],
            now,
            limit,
        ),
    ).fetchall()
    return [_row_to_facet(r) for r in rows]


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
