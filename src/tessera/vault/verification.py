"""Verification checklist facet CRUD per ADR 0018.

A ``verification_checklist`` is the pre-delivery gate an autonomous
worker runs before declaring a task done. The structured metadata
records which agent profile the gate belongs to (``agent_ref``),
when it should run (``trigger``), the list of checks with their
severities (``checks[]``), and free-form pass criteria.

Three severities are fixed in the v0.5 vocabulary:

* ``blocker`` — a failing blocker fails the gate; delivery aborts.
* ``warning`` — surfaces to the user without blocking.
* ``informational`` — annotates the run.

This module owns:

* Validation of the closed metadata shape (``validate_metadata``).
* Insert through ``vault.capture.capture`` so the standard
  ``facet_inserted`` audit row lands beside every other facet type.
* Lookup helpers for ``recall`` and the canonical-checklist tool —
  ``get`` by external_id, ``list_for_agent`` for browse, and
  ``get_canonical_for_agent`` which resolves an
  ``agent_profile.verification_ref`` to the live checklist row.

Verification is a run-gate, not a guarantee. Tessera stores the
checklist; the agent (or its caller-side runner) reads it and
executes the checks. ``pass_criteria`` is documentation, not
enforcement — see ADR 0018 §Boundary statement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

import sqlcipher3

from tessera.vault import capture as vault_capture

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"agent_ref", "trigger", "checks", "pass_criteria"}
)
_PERMITTED_KEYS: Final[frozenset[str]] = _REQUIRED_KEYS

_CHECK_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({"id", "statement", "severity"})
_PERMITTED_SEVERITIES: Final[frozenset[str]] = frozenset({"blocker", "warning", "informational"})

_MAX_TRIGGER_CHARS: Final[int] = 256
_MAX_PASS_CRITERIA_CHARS: Final[int] = 1_024
_MAX_CHECK_ID_CHARS: Final[int] = 128
_MAX_CHECK_STATEMENT_CHARS: Final[int] = 1_024
_MAX_CHECKS: Final[int] = 64

_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class VerificationError(Exception):
    """Base class for verification-checklist failures."""


class InvalidChecklistMetadataError(VerificationError):
    """Metadata shape does not match the ADR 0018 contract."""


@dataclass(frozen=True, slots=True)
class CheckItem:
    """One row in a checklist's ``checks`` list."""

    id: str
    statement: str
    severity: str


@dataclass(frozen=True, slots=True)
class ChecklistMetadata:
    """Validated metadata payload for a verification_checklist facet row."""

    agent_ref: str
    trigger: str
    checks: tuple[CheckItem, ...]
    pass_criteria: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_ref": self.agent_ref,
            "trigger": self.trigger,
            "checks": [
                {"id": c.id, "statement": c.statement, "severity": c.severity} for c in self.checks
            ],
            "pass_criteria": self.pass_criteria,
        }


@dataclass(frozen=True, slots=True)
class Checklist:
    """Read view of one verification_checklist facet row."""

    facet_id: int
    external_id: str
    agent_id: int
    content: str
    captured_at: int
    embed_status: str
    metadata: ChecklistMetadata


def validate_metadata(metadata: dict[str, Any]) -> ChecklistMetadata:
    """Validate a raw metadata dict and freeze it.

    Raises :class:`InvalidChecklistMetadataError` for any shape
    violation. Each error names the offending field so the MCP
    boundary can surface it as ``invalid_input`` without echoing the
    full payload back to the caller.
    """

    if not isinstance(metadata, dict):
        raise InvalidChecklistMetadataError(
            f"metadata must be a dict, got {type(metadata).__name__}"
        )
    extra = set(metadata.keys()) - _PERMITTED_KEYS
    if extra:
        raise InvalidChecklistMetadataError(
            f"metadata carries unknown keys {sorted(extra)}; "
            f"permitted keys: {sorted(_PERMITTED_KEYS)}"
        )
    missing = _REQUIRED_KEYS - set(metadata.keys())
    if missing:
        raise InvalidChecklistMetadataError(f"metadata missing required keys {sorted(missing)}")
    agent_ref = metadata["agent_ref"]
    if not isinstance(agent_ref, str) or not _ULID_PATTERN.match(agent_ref):
        raise InvalidChecklistMetadataError("agent_ref must be a ULID string")
    trigger = _require_short_string(metadata, "trigger", _MAX_TRIGGER_CHARS)
    pass_criteria = _require_short_string(metadata, "pass_criteria", _MAX_PASS_CRITERIA_CHARS)
    checks = _parse_checks(metadata["checks"])
    return ChecklistMetadata(
        agent_ref=agent_ref,
        trigger=trigger,
        checks=checks,
        pass_criteria=pass_criteria,
    )


def register(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    content: str,
    metadata: dict[str, Any],
    source_tool: str,
    captured_at: int | None = None,
) -> tuple[str, bool]:
    """Insert a verification_checklist facet.

    Returns ``(external_id, is_new)``. Routes through
    ``vault.capture.capture`` so the ``facet_inserted`` audit row
    lands beside every other facet type. The caller is responsible
    for separately updating an ``agent_profile.verification_ref`` if
    they want this checklist to be the canonical pre-delivery gate
    for the referenced profile — that mutation is a profile-side
    concern handled by re-registering the profile.
    """

    validate_metadata(metadata)
    result = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="verification_checklist",
        content=content,
        source_tool=source_tool,
        metadata=metadata,
        captured_at=captured_at,
    )
    return result.external_id, not result.is_duplicate


def get(conn: sqlcipher3.Connection, *, external_id: str) -> Checklist | None:
    """Look up one checklist by external_id.

    Returns ``None`` when the row does not exist or has been
    soft-deleted. Cross-agent reads are blocked at the MCP boundary
    by the scope check + an explicit agent-id guard; this storage-
    layer helper returns whatever row matches the external_id.
    """

    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE external_id = ? AND facet_type = 'verification_checklist'
        """,
        (external_id,),
    ).fetchone()
    if row is None or bool(row[7]):
        return None
    return _row_to_checklist(row)


def list_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    limit: int = 20,
) -> list[Checklist]:
    """List checklist facets owned by ``agent_id`` ordered by capture DESC."""

    rows = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE agent_id = ? AND facet_type = 'verification_checklist'
          AND is_deleted = 0
        ORDER BY captured_at DESC, id DESC
        LIMIT ?
        """,
        (agent_id, limit),
    ).fetchall()
    return [_row_to_checklist(r) for r in rows]


def get_canonical_for_profile(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    profile_external_id: str,
) -> Checklist | None:
    """Resolve an ``agent_profile.verification_ref`` to a live checklist.

    Returns the checklist whose ``external_id`` matches the profile's
    ``verification_ref`` *and* whose ``agent_id`` matches the caller.
    Cross-agent leakage is blocked here so the MCP tool can return
    ``None`` rather than fabricating a matching row.
    """

    profile_row = conn.execute(
        """
        SELECT metadata FROM facets
        WHERE external_id = ? AND facet_type = 'agent_profile'
              AND agent_id = ? AND is_deleted = 0
        """,
        (profile_external_id, agent_id),
    ).fetchone()
    if profile_row is None:
        return None
    try:
        meta = json.loads(profile_row[0]) if profile_row[0] else {}
    except json.JSONDecodeError:
        return None
    verification_ref = meta.get("verification_ref")
    if not isinstance(verification_ref, str) or not verification_ref:
        return None
    checklist = get(conn, external_id=verification_ref)
    if checklist is None or checklist.agent_id != agent_id:
        return None
    return checklist


def _row_to_checklist(row: tuple[Any, ...]) -> Checklist:
    raw_metadata = json.loads(row[5]) if row[5] else {}
    metadata = validate_metadata(raw_metadata)
    return Checklist(
        facet_id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        content=str(row[3]),
        captured_at=int(row[4]),
        embed_status=str(row[6]),
        metadata=metadata,
    )


def _parse_checks(value: Any) -> tuple[CheckItem, ...]:
    if not isinstance(value, list):
        raise InvalidChecklistMetadataError(f"checks must be a list, got {type(value).__name__}")
    if not value:
        raise InvalidChecklistMetadataError("checks must contain at least one item")
    if len(value) > _MAX_CHECKS:
        raise InvalidChecklistMetadataError(f"checks has {len(value)} entries; max {_MAX_CHECKS}")
    seen_ids: set[str] = set()
    out: list[CheckItem] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise InvalidChecklistMetadataError(
                f"checks[{index}] must be an object, got {type(raw).__name__}"
            )
        extra = set(raw.keys()) - _CHECK_REQUIRED_KEYS
        if extra:
            raise InvalidChecklistMetadataError(
                f"checks[{index}] carries unknown keys {sorted(extra)}; "
                f"permitted keys: {sorted(_CHECK_REQUIRED_KEYS)}"
            )
        missing = _CHECK_REQUIRED_KEYS - set(raw.keys())
        if missing:
            raise InvalidChecklistMetadataError(
                f"checks[{index}] missing required keys {sorted(missing)}"
            )
        check_id = _entry_short_string(raw["id"], f"checks[{index}].id", _MAX_CHECK_ID_CHARS)
        statement = _entry_short_string(
            raw["statement"], f"checks[{index}].statement", _MAX_CHECK_STATEMENT_CHARS
        )
        severity = raw["severity"]
        if severity not in _PERMITTED_SEVERITIES:
            raise InvalidChecklistMetadataError(
                f"checks[{index}].severity {severity!r} not in {sorted(_PERMITTED_SEVERITIES)}"
            )
        if check_id in seen_ids:
            raise InvalidChecklistMetadataError(
                f"checks[{index}].id {check_id!r} duplicates an earlier check id"
            )
        seen_ids.add(check_id)
        out.append(CheckItem(id=check_id, statement=statement, severity=severity))
    return tuple(out)


def _require_short_string(metadata: dict[str, Any], key: str, max_chars: int) -> str:
    value = metadata.get(key)
    return _entry_short_string(value, key, max_chars)


def _entry_short_string(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidChecklistMetadataError(f"{label} must be a string, got {type(value).__name__}")
    if not value:
        raise InvalidChecklistMetadataError(f"{label} must be non-empty")
    if len(value) > max_chars:
        raise InvalidChecklistMetadataError(f"{label} length {len(value)} exceeds max {max_chars}")
    return value


__all__ = [
    "CheckItem",
    "Checklist",
    "ChecklistMetadata",
    "InvalidChecklistMetadataError",
    "VerificationError",
    "get",
    "get_canonical_for_profile",
    "list_for_agent",
    "register",
    "validate_metadata",
]
