"""Compiled notebook (AgenticOS Playbook) facet CRUD per ADR 0019.

A ``compiled_notebook`` facet pairs with a ``compiled_artifacts``
row. Both are written together in one transaction by
:func:`register_compiled_artifact` — the facet carries the
recallable surface (so SWCR cross-facet bundles can include the
playbook alongside its source facets); the ``compiled_artifacts``
row carries the rendered narrative + the source-facet provenance
list + the compiler version.

ADR 0019 §Boundary statement: **Tessera stores compiled artifacts;
the caller compiles them.** No in-process LLM, no compiler runtime
in the daemon. The two-call API (read sources via ``recall`` /
:func:`list_for_compilation`, then write via
:func:`register_compiled_artifact`) lets any caller pick its own
compiler. The ``register_compiled_artifact`` call is the only write
path — there is no ``compile_now()`` API and no auto-compile.

V0.5-P4 commits the ``is_stale`` field on ``compiled_artifacts``
(default 0). V0.5-P6 wires :func:`mark_stale_for_source` into the
three source-mutation paths (capture, soft-delete, skill procedure
update) so an artifact flips to ``is_stale = 1`` the moment one of
its declared source ULIDs is touched. ADR 0019 §Rationale (6) keeps
this to a flag, not an auto-recompile trigger: Tessera flags, the
compiler decides. Direct membership only — no transitive walk
across the source's own metadata graph (V0.5 scope decision).

The schema name (``compiled_notebook``) is the original ADR 0010
reservation. User-facing prose calls the artifact "the Playbook"
per ADR 0019 §Rationale (3); internal module / facet / table names
keep the original spelling for backward compatibility with the v2
schema CHECK reservation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlcipher3
from ulid import ULID

from tessera.vault import audit_chain
from tessera.vault import facets as vault_facets
from tessera.vault.facets import content_hash

_FACET_TYPE: Final[str] = "compiled_notebook"
_MAX_ARTIFACT_TYPE_CHARS: Final[int] = 64
_MAX_COMPILER_VERSION_CHARS: Final[int] = 128
_MAX_SOURCE_FACETS: Final[int] = 256
_DEFAULT_ARTIFACT_TYPE: Final[str] = "playbook"


class CompiledArtifactError(Exception):
    """Base class for compiled-artifact failures."""


class InvalidCompiledArtifactError(CompiledArtifactError):
    """The supplied compile inputs do not match the ADR 0019 contract."""


class DuplicateCompiledArtifactError(CompiledArtifactError):
    """An artifact with the supplied external_id already exists."""


@dataclass(frozen=True, slots=True)
class CompiledArtifact:
    """Read view pairing the ``compiled_artifacts`` row with its facet."""

    external_id: str
    agent_id: int
    artifact_type: str
    content: str
    source_facets: tuple[str, ...]
    compiled_at: int
    compiler_version: str
    is_stale: bool
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompileSource:
    """A facet eligible to feed a compile target.

    Returned by :func:`list_for_compilation`. The compiler-side
    runner consumes these to assemble its prompt; Tessera does
    nothing with them beyond honest enumeration.
    """

    external_id: str
    facet_type: str
    content: str
    captured_at: int
    metadata: dict[str, Any]


def register_compiled_artifact(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    content: str,
    source_facets: Sequence[str],
    artifact_type: str = _DEFAULT_ARTIFACT_TYPE,
    compiler_version: str,
    source_tool: str,
    metadata: dict[str, Any] | None = None,
    captured_at: int | None = None,
) -> str:
    """Pair-write a compiled artifact and its matching facet.

    Returns the shared ``external_id``. Both rows live in one
    SAVEPOINT so a crash between the two writes leaves neither.
    Inserts the ``compiled_notebook`` facet through the standard
    capture path (which goes through the chain-aware audit insert)
    and the matching ``compiled_artifacts`` row directly.

    Source facets are stored as a JSON array on
    ``compiled_artifacts.source_facets`` so verification can
    re-walk the provenance without a second query. The set is
    bounded so a runaway compiler cannot land an artifact with a
    pathological source list.
    """

    artifact_type = _entry_short_string(artifact_type, "artifact_type", _MAX_ARTIFACT_TYPE_CHARS)
    compiler_version = _entry_short_string(
        compiler_version, "compiler_version", _MAX_COMPILER_VERSION_CHARS
    )
    sources = _validate_sources(source_facets)
    facet_metadata: dict[str, Any] = {
        "artifact_type": artifact_type,
        "compiler_version": compiler_version,
        "source_facets": list(sources),
    }
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise InvalidCompiledArtifactError(
                f"metadata must be a dict, got {type(metadata).__name__}"
            )
        # Caller-side metadata sits under a nested key so the
        # ADR-0019 contract fields cannot be shadowed by a buggy
        # caller passing the same key at the top level.
        facet_metadata["caller_metadata"] = metadata
    when = captured_at if captured_at is not None else _now_epoch()
    external_id = str(ULID())

    conn.execute("SAVEPOINT register_compiled_artifact")
    try:
        # ADR 0019 §Boundary: Tessera stores; the caller compiles. The
        # caller passes a ``source_facets`` list claiming provenance.
        # Validate the claim against the facets table so the audit
        # row's ``source_count`` cannot drift from reality and so a
        # buggy caller cannot land an artifact that points at another
        # agent's rows or non-existent ULIDs. The check runs inside
        # the savepoint so a mismatch rolls the pair-write cleanly.
        _verify_sources_belong_to_agent(conn, agent_id=agent_id, sources=sources)

        # Insert the facet row directly so we can assign the same
        # external_id to both halves of the pair. ``vault_facets.insert``
        # mints its own ULID; we need both halves to share an id so
        # the recall surface can join them.
        digest = content_hash(content)
        conn.execute(
            """
            INSERT INTO facets(
                external_id, agent_id, facet_type, content, content_hash,
                mode, source_tool, captured_at, metadata
            ) VALUES (?, ?, ?, ?, ?, 'write_time', ?, ?, ?)
            """,
            (
                external_id,
                agent_id,
                _FACET_TYPE,
                content,
                digest,
                source_tool,
                when,
                json.dumps(facet_metadata, sort_keys=True, ensure_ascii=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO compiled_artifacts(
                external_id, agent_id, source_facets, artifact_type,
                content, compiled_at, compiler_version, is_stale, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                external_id,
                agent_id,
                json.dumps(list(sources), ensure_ascii=False),
                artifact_type,
                content,
                when,
                compiler_version,
                json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False),
            ),
        )
        audit_chain.audit_log_append(
            conn,
            op="compiled_artifact_registered",
            actor=source_tool,
            agent_id=agent_id,
            target_external_id=external_id,
            payload={
                "artifact_type": artifact_type,
                "compiler_version": compiler_version,
                "source_count": len(sources),
            },
            at=when,
        )
    except sqlcipher3.IntegrityError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT register_compiled_artifact")
        conn.execute("RELEASE SAVEPOINT register_compiled_artifact")
        if "UNIQUE" in str(exc).upper():
            raise DuplicateCompiledArtifactError(
                f"compiled artifact {external_id!r} already exists"
            ) from exc
        if "FOREIGN KEY" in str(exc).upper():
            raise vault_facets.UnknownAgentError(f"no agent with id {agent_id}") from exc
        raise
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT register_compiled_artifact")
        conn.execute("RELEASE SAVEPOINT register_compiled_artifact")
        raise
    conn.execute("RELEASE SAVEPOINT register_compiled_artifact")
    return external_id


def get(
    conn: sqlcipher3.Connection,
    *,
    external_id: str,
) -> CompiledArtifact | None:
    """Fetch one artifact by external_id.

    Returns ``None`` when the row does not exist or its paired
    ``compiled_notebook`` facet has been soft-deleted. The pair is
    joined on ``compiled_artifacts.external_id =
    facets.external_id`` so a ``forget`` against the facet
    automatically tombstones the artifact (V0.5-P6 / PR #61
    review M1 — single source of truth for tombstone state lives
    on the facet row, no parallel ``compiled_artifacts.is_deleted``
    column). Cross-agent reads are blocked at the MCP boundary by
    an explicit agent-id guard; this storage-layer helper returns
    whatever live pair matches.
    """

    row = conn.execute(
        """
        SELECT a.external_id, a.agent_id, a.source_facets, a.artifact_type,
               a.content, a.compiled_at, a.compiler_version, a.is_stale,
               a.metadata
        FROM compiled_artifacts AS a
        JOIN facets AS f ON f.external_id = a.external_id
        WHERE a.external_id = ?
              AND f.is_deleted = 0
        """,
        (external_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_artifact(row)


def list_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    artifact_type: str | None = None,
    limit: int = 20,
) -> list[CompiledArtifact]:
    """List compiled artifacts owned by ``agent_id``.

    Ordered by ``compiled_at DESC, id DESC`` so the most recent
    compile lands first. ``artifact_type`` filters when supplied so
    a caller looking for "the playbook" can ignore degenerate
    research-synthesis rows.
    """

    if artifact_type is None:
        rows = conn.execute(
            """
            SELECT a.external_id, a.agent_id, a.source_facets, a.artifact_type,
                   a.content, a.compiled_at, a.compiler_version, a.is_stale,
                   a.metadata
            FROM compiled_artifacts AS a
            JOIN facets AS f ON f.external_id = a.external_id
            WHERE a.agent_id = ?
                  AND f.is_deleted = 0
            ORDER BY a.compiled_at DESC, a.id DESC
            LIMIT ?
            """,
            (agent_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT a.external_id, a.agent_id, a.source_facets, a.artifact_type,
                   a.content, a.compiled_at, a.compiler_version, a.is_stale,
                   a.metadata
            FROM compiled_artifacts AS a
            JOIN facets AS f ON f.external_id = a.external_id
            WHERE a.agent_id = ?
                  AND a.artifact_type = ?
                  AND f.is_deleted = 0
            ORDER BY a.compiled_at DESC, a.id DESC
            LIMIT ?
            """,
            (agent_id, artifact_type, limit),
        ).fetchall()
    return [_row_to_artifact(row) for row in rows]


def list_for_compilation(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    target: str,
    limit: int = 64,
) -> list[CompileSource]:
    """Return source facets tagged ``metadata.compile_into = [target]``.

    Per ADR 0019 §Source facet inputs the user (or the calling
    tool) marks a source for inclusion in a compile target by
    setting ``compile_into`` on the source facet's metadata. This
    helper enumerates eligible sources without committing them to
    a parallel membership table. The eligible facet types match
    the ADR's primary inputs: agent_profile, project, skill,
    verification_checklist.
    """

    rows = conn.execute(
        """
        SELECT external_id, facet_type, content, captured_at, metadata
        FROM facets
        WHERE agent_id = ?
          AND is_deleted = 0
          AND facet_type IN ('agent_profile', 'project', 'skill', 'verification_checklist')
          AND EXISTS (
            SELECT 1
            FROM json_each(json_extract(metadata, '$.compile_into'))
            WHERE json_each.value = ?
          )
        ORDER BY captured_at DESC, id DESC
        LIMIT ?
        """,
        (agent_id, target, limit),
    ).fetchall()
    return [_row_to_source(row) for row in rows]


def mark_stale_for_source(
    conn: sqlcipher3.Connection,
    *,
    source_external_id: str,
    source_op: str,
    agent_id: int,
) -> int:
    """Flip ``is_stale`` on every artifact citing ``source_external_id``.

    Walks ``compiled_artifacts.source_facets`` (a JSON array column)
    and finds every live row whose array contains the mutating
    source's ULID. Each match flips from ``is_stale = 0`` to
    ``is_stale = 1`` and emits one ``compiled_artifact_marked_stale``
    audit row through the chain-aware insert path so the cascade is
    forensically reconstructible. Already-stale rows are skipped at
    the WHERE clause so the helper is idempotent — a second mutation
    against the same source never re-emits an audit row for an
    already-flagged artifact.

    Scoped by ``agent_id`` so cross-agent membership cannot cascade.
    A leaked ULID surfaced in another agent's source list never
    flips that agent's artifacts (V0.5-P6 security invariant; tested
    in ``test_compiled_staleness_cross_agent_isolation``).

    ``source_op`` is the canonical mutation label
    (``facet_inserted`` / ``facet_soft_deleted`` /
    ``skill_procedure_updated``) so reading the audit log answers
    "which mutation invalidated which artifact" in one query.

    Direct membership only — ADR 0019 §Rationale (6) and the V0.5
    handoff Open Question (4) reject transitive propagation. A skill
    mutation flips a Playbook only when the skill's ULID is in the
    Playbook's ``source_facets`` directly; an agent_profile that
    references the skill via metadata does not propagate.

    Returns the count of artifacts flipped (zero is the common case
    when the mutation hits a facet no Playbook has cited yet).
    """

    if source_op not in {"facet_inserted", "facet_soft_deleted", "skill_procedure_updated"}:
        raise InvalidCompiledArtifactError(
            f"source_op {source_op!r} is not a recognised staleness trigger"
        )
    rows = conn.execute(
        """
        SELECT external_id
        FROM compiled_artifacts
        WHERE agent_id = ?
              AND is_stale = 0
              AND EXISTS (
                  SELECT 1 FROM json_each(source_facets)
                  WHERE json_each.value = ?
              )
        ORDER BY id ASC
        """,
        (agent_id, source_external_id),
    ).fetchall()
    if not rows:
        return 0
    flipped = 0
    for row in rows:
        artifact_external_id = str(row[0])
        cur = conn.execute(
            """
            UPDATE compiled_artifacts
            SET is_stale = 1
            WHERE external_id = ? AND is_stale = 0
            """,
            (artifact_external_id,),
        )
        if int(cur.rowcount) != 1:
            # Another writer flipped the row between SELECT and
            # UPDATE. Skip — no spurious audit row, idempotency
            # preserved.
            continue
        audit_chain.audit_log_append(
            conn,
            op="compiled_artifact_marked_stale",
            actor="system",
            agent_id=agent_id,
            target_external_id=artifact_external_id,
            payload={
                "source_external_id": source_external_id,
                "source_op": source_op,
            },
        )
        flipped += 1
    return flipped


def _row_to_artifact(row: tuple[Any, ...]) -> CompiledArtifact:
    sources_raw = str(row[2]) if row[2] is not None else "[]"
    try:
        sources_list = json.loads(sources_raw)
    except json.JSONDecodeError:
        sources_list = []
    if not isinstance(sources_list, list):
        sources_list = []
    return CompiledArtifact(
        external_id=str(row[0]),
        agent_id=int(row[1]),
        artifact_type=str(row[3]),
        content=str(row[4]),
        source_facets=tuple(str(s) for s in sources_list if isinstance(s, str)),
        compiled_at=int(row[5]),
        compiler_version=str(row[6]),
        is_stale=bool(row[7]),
        metadata=_decode_metadata(row[8]),
    )


def _row_to_source(row: tuple[Any, ...]) -> CompileSource:
    return CompileSource(
        external_id=str(row[0]),
        facet_type=str(row[1]),
        content=str(row[2]),
        captured_at=int(row[3]),
        metadata=_decode_metadata(row[4]),
    )


def _decode_metadata(raw: Any) -> dict[str, Any]:
    """Best-effort decode of a JSON metadata column into a dict.

    Returns an empty dict for NULL, malformed JSON, or non-object
    payloads. The two row-mapping helpers share this contract so a
    corrupt row never leaks past the storage layer.
    """

    metadata_raw = str(raw) if raw is not None else "{}"
    try:
        decoded = json.loads(metadata_raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _verify_sources_belong_to_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    sources: tuple[str, ...],
) -> None:
    """Raise when any source ULID is not a live facet owned by ``agent_id``.

    A single ``IN (...)`` query gives O(1) round-trips regardless of
    source-list size. Soft-deleted rows are excluded so the
    provenance list cannot point at tombstones; cross-agent rows are
    excluded by the ``agent_id`` filter so a write-scoped caller
    cannot plant a Playbook claiming sources owned by another agent.
    The check runs inside the pair-write savepoint so a mismatch
    rolls the entire transaction cleanly.
    """

    placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT external_id FROM facets
        WHERE external_id IN ({placeholders})
              AND agent_id = ?
              AND is_deleted = 0
        """,
        (*sources, agent_id),
    ).fetchall()
    found = {str(row[0]) for row in rows}
    missing = [src for src in sources if src not in found]
    if missing:
        raise InvalidCompiledArtifactError(
            f"source_facets reference rows that are missing, soft-deleted, "
            f"or owned by another agent: {missing[:5]}"
            + (f" (and {len(missing) - 5} more)" if len(missing) > 5 else "")
        )


def _validate_sources(sources: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(sources, list | tuple):
        raise InvalidCompiledArtifactError(
            f"source_facets must be a list, got {type(sources).__name__}"
        )
    if not sources:
        raise InvalidCompiledArtifactError("source_facets must contain at least one entry")
    if len(sources) > _MAX_SOURCE_FACETS:
        raise InvalidCompiledArtifactError(
            f"source_facets has {len(sources)} entries; max {_MAX_SOURCE_FACETS}"
        )
    out: list[str] = []
    for index, entry in enumerate(sources):
        if not isinstance(entry, str) or not entry:
            raise InvalidCompiledArtifactError(f"source_facets[{index}] must be a non-empty string")
        out.append(entry)
    return tuple(out)


def _entry_short_string(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidCompiledArtifactError(f"{label} must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidCompiledArtifactError(f"{label} must be non-empty")
    if len(value) > max_chars:
        raise InvalidCompiledArtifactError(f"{label} length {len(value)} exceeds max {max_chars}")
    return value


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


__all__ = [
    "CompileSource",
    "CompiledArtifact",
    "CompiledArtifactError",
    "DuplicateCompiledArtifactError",
    "InvalidCompiledArtifactError",
    "get",
    "list_for_agent",
    "list_for_compilation",
    "mark_stale_for_source",
    "register_compiled_artifact",
]
