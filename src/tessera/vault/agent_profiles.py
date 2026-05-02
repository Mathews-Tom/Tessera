"""Agent profile facet CRUD per ADR 0017.

An ``agent_profile`` facet is the durable, recallable description of
what an autonomous worker does — purpose, inputs, outputs, cadence,
the skills it depends on, and an optional verification checklist
reference. The facet sits beside the ``agents`` row that issues the
worker's auth tokens, linked by ``agents.profile_facet_external_id``,
but the two stay distinct concepts: ``agents`` is the JWT subject
store and never gains profile-shaped columns; ``agent_profile`` is
recallable user context handled like every other facet type.

This module owns:

* Insert: validates the metadata shape, writes a facet row through
  :mod:`tessera.vault.facets`, then updates ``agents`` to point at
  the new profile inside the same transaction.
* Get: returns one profile by external_id (or the agent's currently
  linked profile when called via :func:`get_active_for_agent`).
* List: enumerates every profile facet for an agent ordered by
  capture time, with the active link surfaced as a flag so callers
  can render which one is canonical without a second query.
* Active link mutation audit: writes ``agent_profile_link_set`` /
  ``agent_profile_link_cleared`` rows whenever the canonical pointer
  on ``agents`` moves.

Profile content is the human-readable narrative (markdown). Metadata
carries the structured fields per ADR 0017 §Facet shape; validation
enforces a closed shape so SWCR can surface a profile bundled with
its referenced project / verification / skill facets without parsing
free-form prose. Skill / verification refs are stored as ULIDs (the
``external_id`` of the related skill or verification facet) — this
module does not resolve them; that is the recall pipeline's job.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import sqlcipher3

from tessera.vault import audit
from tessera.vault import capture as vault_capture

# Closed metadata shape per ADR 0017 §Facet shape. The outer keys are
# fixed; ``skill_refs`` is a list of ULIDs (each pointing at a
# ``skill`` facet's external_id), ``verification_ref`` is one ULID or
# null. Free-form fields (``purpose``, ``cadence``) are short strings.
_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"purpose", "inputs", "outputs", "cadence", "skill_refs"}
)
_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"verification_ref"})
_PERMITTED_KEYS: Final[frozenset[str]] = _REQUIRED_KEYS | _OPTIONAL_KEYS

_MAX_PURPOSE_CHARS: Final[int] = 512
_MAX_CADENCE_CHARS: Final[int] = 256
_MAX_INPUT_CHARS: Final[int] = 512
_MAX_OUTPUT_CHARS: Final[int] = 512
_MAX_INPUTS_OUTPUTS: Final[int] = 16
_MAX_SKILL_REFS: Final[int] = 32

# ULID shape mirroring tessera.mcp_surface.tools — Crockford base32, 26
# chars, uppercase. Skill / verification refs MUST be ULIDs; the
# ``external_id`` of every facet row is a ULID per
# :func:`tessera.vault.facets.insert`.
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class AgentProfileError(Exception):
    """Base class for agent_profile failures."""


class InvalidAgentProfileMetadataError(AgentProfileError):
    """Metadata shape does not match the ADR 0017 contract."""


class UnknownAgentProfileError(AgentProfileError):
    """Referenced profile external_id does not exist."""


@dataclass(frozen=True, slots=True)
class AgentProfileMetadata:
    """Validated metadata payload for an agent_profile facet row."""

    purpose: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    cadence: str
    skill_refs: tuple[str, ...]
    verification_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "purpose": self.purpose,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "cadence": self.cadence,
            "skill_refs": list(self.skill_refs),
        }
        if self.verification_ref is not None:
            out["verification_ref"] = self.verification_ref
        return out


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """Read view of one agent_profile facet row."""

    facet_id: int
    external_id: str
    agent_id: int
    content: str
    captured_at: int
    embed_status: str
    metadata: AgentProfileMetadata
    is_active_link: bool


def validate_metadata(metadata: dict[str, Any]) -> AgentProfileMetadata:
    """Validate a raw metadata dict and freeze it.

    Raises :class:`InvalidAgentProfileMetadataError` for any shape
    violation. The error messages name the offending field so the MCP
    boundary can surface them as ``invalid_input`` without leaking the
    full payload back to the caller.
    """

    if not isinstance(metadata, dict):
        raise InvalidAgentProfileMetadataError(
            f"metadata must be a dict, got {type(metadata).__name__}"
        )
    extra = set(metadata.keys()) - _PERMITTED_KEYS
    if extra:
        raise InvalidAgentProfileMetadataError(
            f"metadata carries unknown keys {sorted(extra)}; "
            f"permitted keys: {sorted(_PERMITTED_KEYS)}"
        )
    missing = _REQUIRED_KEYS - set(metadata.keys())
    if missing:
        raise InvalidAgentProfileMetadataError(f"metadata missing required keys {sorted(missing)}")
    purpose = _require_short_string(metadata, "purpose", _MAX_PURPOSE_CHARS)
    cadence = _require_short_string(metadata, "cadence", _MAX_CADENCE_CHARS)
    inputs = _require_string_list(metadata, "inputs", _MAX_INPUTS_OUTPUTS, _MAX_INPUT_CHARS)
    outputs = _require_string_list(metadata, "outputs", _MAX_INPUTS_OUTPUTS, _MAX_OUTPUT_CHARS)
    skill_refs = _require_ulid_list(metadata, "skill_refs", _MAX_SKILL_REFS)
    verification_ref = metadata.get("verification_ref")
    if verification_ref is not None and (
        not isinstance(verification_ref, str) or not _ULID_PATTERN.match(verification_ref)
    ):
        raise InvalidAgentProfileMetadataError("verification_ref must be a ULID string or null")
    return AgentProfileMetadata(
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        cadence=cadence,
        skill_refs=skill_refs,
        verification_ref=verification_ref,
    )


def register(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    content: str,
    metadata: dict[str, Any],
    source_tool: str,
    captured_at: int | None = None,
    set_active_link: bool = True,
) -> tuple[str, bool]:
    """Insert an agent_profile facet and optionally mark it canonical.

    Returns ``(external_id, is_new)``. ``is_new=False`` indicates a
    content-hash duplicate of an existing live row was returned (the
    caller wrote the same profile twice); the active-link update still
    runs so a re-register on the same content can re-point a stale
    pointer. When ``set_active_link=False`` the agents row is left
    alone — useful for callers staging a new profile draft without
    immediately swapping the canonical pointer.
    """

    validated = validate_metadata(metadata)
    # Route through ``capture.capture`` so the ``facet_inserted`` audit
    # row lands beside every other facet type. Going straight through
    # ``facets.insert`` would skip the audit emission and leave
    # agent_profile rows invisible to forensic scans of the audit log.
    result = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="agent_profile",
        content=content,
        source_tool=source_tool,
        metadata=validated.to_dict(),
        captured_at=captured_at,
    )
    if set_active_link:
        _set_active_link(conn, agent_id=agent_id, profile_external_id=result.external_id)
    return result.external_id, not result.is_duplicate


def get(
    conn: sqlcipher3.Connection,
    *,
    external_id: str,
) -> AgentProfile | None:
    """Look up one profile by external_id.

    Returns ``None`` when the row does not exist or has been
    soft-deleted; callers wanting tombstone visibility should query
    :mod:`tessera.vault.facets.get` directly. Cross-agent reads are
    rejected at the MCP boundary by the scope check; this module
    surfaces whichever row matches the external_id and leaves
    authorization to the caller.
    """

    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE external_id = ? AND facet_type = 'agent_profile'
        """,
        (external_id,),
    ).fetchone()
    if row is None or bool(row[7]):
        return None
    active_link = _read_active_link(conn, agent_id=int(row[2]))
    return _row_to_profile(row, is_active_link=(active_link == str(row[1])))


def list_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    limit: int = 20,
    since: int | None = None,
) -> list[AgentProfile]:
    """List agent_profile facets owned by ``agent_id``.

    Ordered by ``captured_at DESC`` so the most recently registered
    profile lands first. Soft-deleted rows are filtered out. The
    active-link flag on each row mirrors
    ``agents.profile_facet_external_id`` so a single query gives the
    caller everything needed to render "current" vs "draft / prior".
    """

    if since is None:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, content, captured_at, metadata,
                   embed_status, is_deleted
            FROM facets
            WHERE agent_id = ? AND facet_type = 'agent_profile' AND is_deleted = 0
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (agent_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, content, captured_at, metadata,
                   embed_status, is_deleted
            FROM facets
            WHERE agent_id = ? AND facet_type = 'agent_profile' AND is_deleted = 0
              AND captured_at >= ?
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (agent_id, since, limit),
        ).fetchall()
    active_link = _read_active_link(conn, agent_id=agent_id)
    return [_row_to_profile(r, is_active_link=(active_link == str(r[1]))) for r in rows]


def read_active_link(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
) -> str | None:
    """Return the active-link external_id for ``agent_id`` or ``None``.

    Public surface over :func:`_read_active_link` so callers can
    answer "which profile is canonical right now?" without rerunning
    a list query.
    """

    return _read_active_link(conn, agent_id=agent_id)


def get_active_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
) -> AgentProfile | None:
    """Return the profile currently linked from ``agents.profile_facet_external_id``.

    Returns ``None`` when the agent has not registered a profile yet
    (the column is NULL) or when the linked profile has been
    soft-deleted out from under the link. The latter is a transient
    state the dispatcher can repair by re-registering — this module
    does not auto-clear stale links because the caller might want to
    diagnose how the link drifted.
    """

    active_link = _read_active_link(conn, agent_id=agent_id)
    if active_link is None:
        return None
    return get(conn, external_id=active_link)


def clear_active_link(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
) -> bool:
    """Set ``agents.profile_facet_external_id`` to NULL with audit.

    Returns ``True`` when the link was non-NULL and has been cleared.
    Useful for retiring an agent without leaving a dangling pointer
    when the underlying facet is being soft-deleted in the same
    operation.
    """

    current = _read_active_link(conn, agent_id=agent_id)
    if current is None:
        return False
    conn.execute(
        "UPDATE agents SET profile_facet_external_id = NULL WHERE id = ?",
        (agent_id,),
    )
    audit.write(
        conn,
        op="agent_profile_link_cleared",
        actor="system",
        agent_id=agent_id,
        target_external_id=current,
        payload={},
    )
    return True


def _set_active_link(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    profile_external_id: str,
) -> None:
    """Update ``agents.profile_facet_external_id`` with audit.

    Idempotent on the same target. The audit row records the prior
    pointer (if any) so forensics can reconstruct the link's history
    without scanning the facets table.
    """

    current = _read_active_link(conn, agent_id=agent_id)
    if current == profile_external_id:
        return
    conn.execute(
        "UPDATE agents SET profile_facet_external_id = ? WHERE id = ?",
        (profile_external_id, agent_id),
    )
    audit.write(
        conn,
        op="agent_profile_link_set",
        actor="system",
        agent_id=agent_id,
        target_external_id=profile_external_id,
        payload={"prior_external_id": current},
    )


def _read_active_link(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
) -> str | None:
    row = conn.execute(
        "SELECT profile_facet_external_id FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _row_to_profile(row: tuple[Any, ...], *, is_active_link: bool) -> AgentProfile:
    raw_metadata = json.loads(row[5]) if row[5] else {}
    metadata = validate_metadata(raw_metadata)
    return AgentProfile(
        facet_id=int(row[0]),
        external_id=str(row[1]),
        agent_id=int(row[2]),
        content=str(row[3]),
        captured_at=int(row[4]),
        embed_status=str(row[6]),
        metadata=metadata,
        is_active_link=is_active_link,
    )


def _require_short_string(metadata: dict[str, Any], key: str, max_chars: int) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] must be a string, got {type(value).__name__}"
        )
    if not value:
        raise InvalidAgentProfileMetadataError(f"metadata['{key}'] must be non-empty")
    if len(value) > max_chars:
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] length {len(value)} exceeds max {max_chars}"
        )
    return value


def _require_string_list(
    metadata: dict[str, Any],
    key: str,
    max_items: int,
    max_chars: int,
) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list):
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] must be a list, got {type(value).__name__}"
        )
    if len(value) > max_items:
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] has {len(value)} entries; max {max_items}"
        )
    return tuple(_validate_string_entry(value, key, max_chars))


def _require_ulid_list(
    metadata: dict[str, Any],
    key: str,
    max_items: int,
) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list):
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] must be a list, got {type(value).__name__}"
        )
    if len(value) > max_items:
        raise InvalidAgentProfileMetadataError(
            f"metadata['{key}'] has {len(value)} entries; max {max_items}"
        )
    out: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not _ULID_PATTERN.match(entry):
            raise InvalidAgentProfileMetadataError(
                f"metadata['{key}'][{index}] must be a ULID string"
            )
        out.append(entry)
    return tuple(out)


def _validate_string_entry(
    items: Sequence[Any],
    key: str,
    max_chars: int,
) -> Iterable[str]:
    for index, entry in enumerate(items):
        if not isinstance(entry, str):
            raise InvalidAgentProfileMetadataError(
                f"metadata['{key}'][{index}] must be a string, got {type(entry).__name__}"
            )
        if not entry:
            raise InvalidAgentProfileMetadataError(f"metadata['{key}'][{index}] must be non-empty")
        if len(entry) > max_chars:
            raise InvalidAgentProfileMetadataError(
                f"metadata['{key}'][{index}] length {len(entry)} exceeds max {max_chars}"
            )
        yield entry


__all__ = [
    "AgentProfile",
    "AgentProfileError",
    "AgentProfileMetadata",
    "InvalidAgentProfileMetadataError",
    "UnknownAgentProfileError",
    "clear_active_link",
    "get",
    "get_active_for_agent",
    "list_for_agent",
    "read_active_link",
    "register",
    "validate_metadata",
]
