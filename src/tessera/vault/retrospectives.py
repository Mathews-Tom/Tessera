"""Retrospective facet CRUD per ADR 0018.

A ``retrospective`` is the post-run record of how an autonomous
worker performed on one task: what went well, what gaps surfaced,
what should change next time, and the outcome bucket
(success / partial / failure). The structured metadata names which
``agent_profile`` the run belonged to (``agent_ref``) and the
caller-supplied identifier of the task (``task_id``).

This module is the storage layer; the SWCR retrospective
augmentation in ``tessera.retrieval.pipeline`` consumes
:func:`recent_for_agent` to surface the most recent N retrospectives
whenever an ``agent_profile`` facet enters the candidate set.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

import sqlcipher3

from tessera.vault import capture as vault_capture

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"agent_ref", "task_id", "went_well", "gaps", "changes", "outcome"}
)
_PERMITTED_KEYS: Final[frozenset[str]] = _REQUIRED_KEYS

_PERMITTED_OUTCOMES: Final[frozenset[str]] = frozenset({"success", "partial", "failure"})
_CHANGE_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({"target", "change"})

_MAX_TASK_ID_CHARS: Final[int] = 256
_MAX_LINE_CHARS: Final[int] = 1_024
_MAX_LINES: Final[int] = 64
_MAX_CHANGES: Final[int] = 64
_MAX_CHANGE_TARGET_CHARS: Final[int] = 256
_MAX_CHANGE_TEXT_CHARS: Final[int] = 1_024

_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class RetrospectiveError(Exception):
    """Base class for retrospective failures."""


class InvalidRetrospectiveMetadataError(RetrospectiveError):
    """Metadata shape does not match the ADR 0018 contract."""


@dataclass(frozen=True, slots=True)
class ChangeItem:
    target: str
    change: str


@dataclass(frozen=True, slots=True)
class RetrospectiveMetadata:
    """Validated metadata payload for a retrospective facet row."""

    agent_ref: str
    task_id: str
    went_well: tuple[str, ...]
    gaps: tuple[str, ...]
    changes: tuple[ChangeItem, ...]
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_ref": self.agent_ref,
            "task_id": self.task_id,
            "went_well": list(self.went_well),
            "gaps": list(self.gaps),
            "changes": [{"target": c.target, "change": c.change} for c in self.changes],
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class Retrospective:
    """Read view of one retrospective facet row."""

    facet_id: int
    external_id: str
    agent_id: int
    content: str
    captured_at: int
    embed_status: str
    metadata: RetrospectiveMetadata


def validate_metadata(metadata: dict[str, Any]) -> RetrospectiveMetadata:
    """Validate a raw metadata dict and freeze it.

    Raises :class:`InvalidRetrospectiveMetadataError` for any shape
    violation. Each error names the offending field so the MCP
    boundary surfaces it as ``invalid_input`` without echoing the
    full payload back.
    """

    if not isinstance(metadata, dict):
        raise InvalidRetrospectiveMetadataError(
            f"metadata must be a dict, got {type(metadata).__name__}"
        )
    extra = set(metadata.keys()) - _PERMITTED_KEYS
    if extra:
        raise InvalidRetrospectiveMetadataError(
            f"metadata carries unknown keys {sorted(extra)}; "
            f"permitted keys: {sorted(_PERMITTED_KEYS)}"
        )
    missing = _REQUIRED_KEYS - set(metadata.keys())
    if missing:
        raise InvalidRetrospectiveMetadataError(f"metadata missing required keys {sorted(missing)}")
    agent_ref = metadata["agent_ref"]
    if not isinstance(agent_ref, str) or not _ULID_PATTERN.match(agent_ref):
        raise InvalidRetrospectiveMetadataError("agent_ref must be a ULID string")
    task_id = _entry_short_string(metadata["task_id"], "task_id", _MAX_TASK_ID_CHARS)
    went_well = _string_list(metadata["went_well"], "went_well")
    gaps = _string_list(metadata["gaps"], "gaps")
    changes = _parse_changes(metadata["changes"])
    outcome = metadata["outcome"]
    if outcome not in _PERMITTED_OUTCOMES:
        raise InvalidRetrospectiveMetadataError(
            f"outcome {outcome!r} not in {sorted(_PERMITTED_OUTCOMES)}"
        )
    return RetrospectiveMetadata(
        agent_ref=agent_ref,
        task_id=task_id,
        went_well=went_well,
        gaps=gaps,
        changes=changes,
        outcome=outcome,
    )


def record(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    content: str,
    metadata: dict[str, Any],
    source_tool: str,
    captured_at: int | None = None,
) -> tuple[str, bool]:
    """Insert a retrospective facet.

    Returns ``(external_id, is_new)``. Routes through
    ``vault.capture.capture`` so the ``facet_inserted`` audit row
    lands beside every other facet type. Retrospectives are
    immutable per task by design — re-recording for the same
    ``task_id`` writes a new row whose deduplication via
    ``content_hash`` collapses byte-identical re-records and surfaces
    real edits as new rows.
    """

    validate_metadata(metadata)
    result = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="retrospective",
        content=content,
        source_tool=source_tool,
        metadata=metadata,
        captured_at=captured_at,
    )
    return result.external_id, not result.is_duplicate


def get(conn: sqlcipher3.Connection, *, external_id: str) -> Retrospective | None:
    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE external_id = ? AND facet_type = 'retrospective'
        """,
        (external_id,),
    ).fetchone()
    if row is None or bool(row[7]):
        return None
    return _row_to_retrospective(row)


def recent_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    profile_external_id: str,
    limit: int,
) -> list[Retrospective]:
    """Most recent retrospectives whose ``agent_ref`` matches the profile.

    Used by SWCR's retrospective augmentation rule (ADR 0018
    §SWCR retrospective integration). Filters by ``agent_id`` to
    keep the cross-agent boundary; orders by ``captured_at DESC, id
    DESC`` so reruns at the same epoch second still produce a stable
    sort.
    """

    if limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE agent_id = ? AND facet_type = 'retrospective'
              AND is_deleted = 0
              AND json_extract(metadata, '$.agent_ref') = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT ?
        """,
        (agent_id, profile_external_id, limit),
    ).fetchall()
    return [_row_to_retrospective(r) for r in rows]


def _row_to_retrospective(row: tuple[Any, ...]) -> Retrospective:
    raw_metadata = json.loads(row[5]) if row[5] else {}
    metadata = validate_metadata(raw_metadata)
    return Retrospective(
        facet_id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        content=str(row[3]),
        captured_at=int(row[4]),
        embed_status=str(row[6]),
        metadata=metadata,
    )


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidRetrospectiveMetadataError(
            f"{label} must be a list, got {type(value).__name__}"
        )
    if len(value) > _MAX_LINES:
        raise InvalidRetrospectiveMetadataError(
            f"{label} has {len(value)} entries; max {_MAX_LINES}"
        )
    out: list[str] = []
    for index, entry in enumerate(value):
        out.append(_entry_short_string(entry, f"{label}[{index}]", _MAX_LINE_CHARS))
    return tuple(out)


def _parse_changes(value: Any) -> tuple[ChangeItem, ...]:
    if not isinstance(value, list):
        raise InvalidRetrospectiveMetadataError(
            f"changes must be a list, got {type(value).__name__}"
        )
    if len(value) > _MAX_CHANGES:
        raise InvalidRetrospectiveMetadataError(
            f"changes has {len(value)} entries; max {_MAX_CHANGES}"
        )
    out: list[ChangeItem] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise InvalidRetrospectiveMetadataError(
                f"changes[{index}] must be an object, got {type(raw).__name__}"
            )
        extra = set(raw.keys()) - _CHANGE_REQUIRED_KEYS
        if extra:
            raise InvalidRetrospectiveMetadataError(
                f"changes[{index}] carries unknown keys {sorted(extra)}; "
                f"permitted keys: {sorted(_CHANGE_REQUIRED_KEYS)}"
            )
        missing = _CHANGE_REQUIRED_KEYS - set(raw.keys())
        if missing:
            raise InvalidRetrospectiveMetadataError(
                f"changes[{index}] missing required keys {sorted(missing)}"
            )
        target = _entry_short_string(
            raw["target"], f"changes[{index}].target", _MAX_CHANGE_TARGET_CHARS
        )
        change_text = _entry_short_string(
            raw["change"], f"changes[{index}].change", _MAX_CHANGE_TEXT_CHARS
        )
        out.append(ChangeItem(target=target, change=change_text))
    return tuple(out)


def _entry_short_string(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidRetrospectiveMetadataError(
            f"{label} must be a string, got {type(value).__name__}"
        )
    if not value:
        raise InvalidRetrospectiveMetadataError(f"{label} must be non-empty")
    if len(value) > max_chars:
        raise InvalidRetrospectiveMetadataError(
            f"{label} length {len(value)} exceeds max {max_chars}"
        )
    return value


__all__ = [
    "ChangeItem",
    "InvalidRetrospectiveMetadataError",
    "Retrospective",
    "RetrospectiveError",
    "RetrospectiveMetadata",
    "get",
    "recent_for_agent",
    "record",
    "validate_metadata",
]
