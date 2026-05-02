"""Automation registry facet CRUD per ADR 0020.

An ``automation`` facet is a durable, recallable record of a
scheduled-or-triggered task that some caller-side runner owns.
Tessera **stores** the automation; runners (Claude Code's
``/schedule``, OpenClaw's HEARTBEAT, cron, systemd timers, GitHub
Actions, custom shell loops) **execute** them.

The boundary is non-negotiable per ADR 0020 §Boundary statement:
the daemon ships no scheduler runtime, no outbound trigger, no
in-process timer. The registry is portable because it is opaque to
runners — ``trigger_spec`` is a free-form string the runner parses;
``runner`` is a free-form identifier the runner self-declares;
neither is interpreted by Tessera. A new runner emerges and the
registry accommodates it without code change.

Two write paths live here:

* :func:`register` — insert an automation facet through
  ``vault.capture.capture`` so the standard ``facet_inserted`` audit
  row lands beside every other facet type.
* :func:`record_run` — update the existing row's ``last_run`` and
  ``last_result`` metadata fields after a runner fires. This is the
  only metadata-mutation path on the registry; it emits a dedicated
  ``automation_run_recorded`` audit row through the V0.5-P8 chain
  insert so forensics can reconstruct run history without inferring
  it from the (lossy) overwritten metadata.

Read paths reuse the generic surface — ``recall``, ``list_facets``,
``show`` — per ADR 0020 §Rationale 3. The two read helpers below
(:func:`get`, :func:`list_for_agent`) are storage-layer primitives
the MCP / REST surface calls.

ADR 0020 §Rationale 5 explicitly omits ``next_run``: a computed
next-run timestamp would imply Tessera knows when to fire. The
runner owns the future; the registry records the past
(``last_run``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

import sqlcipher3

from tessera.vault import audit
from tessera.vault import capture as vault_capture

_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"agent_ref", "trigger_spec", "cadence", "runner"}
)
_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"last_run", "last_result"})
_PERMITTED_KEYS: Final[frozenset[str]] = _REQUIRED_KEYS | _OPTIONAL_KEYS

# ``last_result`` is intentionally semi-open: ADR 0020 §Facet shape
# names ``success | partial | failure | string`` so the runner can
# carry a free-form note when the three buckets do not capture the
# nuance ("partial: 3/5 sources scraped"). The closed allowlist
# applies only to the structured-bucket case; any non-bucket value
# is treated as an opaque short string.
_RESULT_BUCKETS: Final[frozenset[str]] = frozenset({"success", "partial", "failure"})

_MAX_TRIGGER_SPEC_CHARS: Final[int] = 1_024
_MAX_CADENCE_CHARS: Final[int] = 256
_MAX_RUNNER_CHARS: Final[int] = 128
_MAX_LAST_RUN_CHARS: Final[int] = 64
_MAX_LAST_RESULT_CHARS: Final[int] = 1_024

_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ISO8601_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


class AutomationError(Exception):
    """Base class for automation-registry failures."""


class InvalidAutomationMetadataError(AutomationError):
    """Metadata shape does not match the ADR 0020 contract."""


class UnknownAutomationError(AutomationError):
    """Referenced automation external_id does not exist (or belongs to another agent)."""


class CorruptAutomationRowError(AutomationError):
    """Stored automation row's metadata column is malformed.

    Distinct from :class:`InvalidAutomationMetadataError` (caller
    input is bad) so the MCP boundary can map this to ``StorageError``
    (the vault state is bad, not the caller). Raised by
    :func:`record_run` and the row-mapping helpers when the
    persisted JSON cannot be decoded or fails post-load
    re-validation.
    """


@dataclass(frozen=True, slots=True)
class AutomationMetadata:
    """Validated metadata payload for one ``automation`` facet row."""

    agent_ref: str
    trigger_spec: str
    cadence: str
    runner: str
    last_run: str | None
    last_result: str | None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "agent_ref": self.agent_ref,
            "trigger_spec": self.trigger_spec,
            "cadence": self.cadence,
            "runner": self.runner,
        }
        if self.last_run is not None:
            out["last_run"] = self.last_run
        if self.last_result is not None:
            out["last_result"] = self.last_result
        return out


@dataclass(frozen=True, slots=True)
class Automation:
    """Read view of one ``automation`` facet row."""

    facet_id: int
    external_id: str
    agent_id: int
    content: str
    captured_at: int
    embed_status: str
    metadata: AutomationMetadata


def validate_metadata(metadata: dict[str, Any]) -> AutomationMetadata:
    """Validate a raw metadata dict against the ADR 0020 contract.

    Raises :class:`InvalidAutomationMetadataError` for any shape
    violation. Each error names the offending field so the MCP
    boundary surfaces it as ``invalid_input`` without echoing the
    full payload back. ``last_run`` / ``last_result`` are optional
    on register; the typical first-write omits them and the runner
    fills them via :func:`record_run` after the first fire.
    """

    if not isinstance(metadata, dict):
        raise InvalidAutomationMetadataError(
            f"metadata must be a dict, got {type(metadata).__name__}"
        )
    extra = set(metadata.keys()) - _PERMITTED_KEYS
    if extra:
        raise InvalidAutomationMetadataError(
            f"metadata carries unknown keys {sorted(extra)}; "
            f"permitted keys: {sorted(_PERMITTED_KEYS)}"
        )
    missing = _REQUIRED_KEYS - set(metadata.keys())
    if missing:
        raise InvalidAutomationMetadataError(f"metadata missing required keys {sorted(missing)}")
    agent_ref = metadata["agent_ref"]
    if not isinstance(agent_ref, str) or not _ULID_PATTERN.match(agent_ref):
        raise InvalidAutomationMetadataError("agent_ref must be a ULID string")
    trigger_spec = _entry_short_string(
        metadata["trigger_spec"], "trigger_spec", _MAX_TRIGGER_SPEC_CHARS
    )
    cadence = _entry_short_string(metadata["cadence"], "cadence", _MAX_CADENCE_CHARS)
    runner = _entry_short_string(metadata["runner"], "runner", _MAX_RUNNER_CHARS)
    last_run = _optional_iso_timestamp(metadata.get("last_run"))
    last_result = _optional_short_string(
        metadata.get("last_result"), "last_result", _MAX_LAST_RESULT_CHARS
    )
    return AutomationMetadata(
        agent_ref=agent_ref,
        trigger_spec=trigger_spec,
        cadence=cadence,
        runner=runner,
        last_run=last_run,
        last_result=last_result,
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
    """Insert an automation facet.

    Returns ``(external_id, is_new)``. Routes through
    ``vault.capture.capture`` so the ``facet_inserted`` audit row
    and the V0.5-P6 staleness hook fire uniformly across facet
    types. Re-registering an automation whose content + metadata
    hash to the same ``content_hash`` collapses to the prior row
    (V0.1 dedup); meaningful edits change the content and produce
    a fresh row.
    """

    validate_metadata(metadata)
    result = vault_capture.capture(
        conn,
        agent_id=agent_id,
        facet_type="automation",
        content=content,
        source_tool=source_tool,
        metadata=metadata,
        captured_at=captured_at,
    )
    return result.external_id, not result.is_duplicate


def record_run(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    external_id: str,
    last_run: str,
    last_result: str,
) -> bool:
    """Update ``last_run`` and ``last_result`` on an existing automation.

    Returns True when the row was found and updated; raises
    :class:`UnknownAutomationError` when the external_id does not
    resolve to a live automation owned by ``agent_id``. The
    cross-agent guard runs at the storage layer in addition to the
    MCP-layer scope check so a buggy caller cannot mutate another
    agent's registry by guessing a ULID.

    The metadata column is rewritten with the new timestamp + result
    in place; prior values are overwritten (the audit chain holds the
    history). ``content`` and ``content_hash`` are unchanged so
    re-registering the same automation later still dedups against the
    original content; only ``metadata`` and the audit row reflect the
    run.

    Emits one ``automation_run_recorded`` audit row carrying
    ``{result_bucket, last_run_at}`` per ``docs/threat-model.md``
    §S4 boundary — free-form ``last_result`` notes never enter the
    audit payload; only the bucketed canonical (or ``"other"`` for
    non-bucket values) is recorded so forensics can summarise run
    history without leaking caller-supplied prose.
    """

    last_run = _entry_short_string(last_run, "last_run", _MAX_LAST_RUN_CHARS)
    if not _ISO8601_PATTERN.match(last_run):
        raise InvalidAutomationMetadataError(
            "last_run must be an ISO-8601 timestamp (e.g. '2026-05-02T09:00:00Z')"
        )
    last_result = _entry_short_string(last_result, "last_result", _MAX_LAST_RESULT_CHARS)

    row = conn.execute(
        """
        SELECT metadata FROM facets
        WHERE external_id = ? AND facet_type = 'automation'
              AND agent_id = ? AND is_deleted = 0
        """,
        (external_id, agent_id),
    ).fetchone()
    if row is None:
        raise UnknownAutomationError(
            f"no live automation with external_id {external_id!r} for this agent"
        )
    try:
        existing_meta = json.loads(row[0]) if row[0] else {}
    except json.JSONDecodeError as exc:
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} is not valid JSON"
        ) from exc
    if not isinstance(existing_meta, dict):
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} is not a JSON object"
        )
    new_meta = {**existing_meta, "last_run": last_run, "last_result": last_result}
    # Re-validate so a previously-stored row that drifted from the
    # contract surfaces here rather than silently writing a more
    # invalid shape on top of it. Drift surfaces as
    # ``CorruptAutomationRowError`` so the MCP boundary maps it to
    # ``StorageError`` (vault state is bad, not caller input).
    try:
        validate_metadata(new_meta)
    except InvalidAutomationMetadataError as exc:
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} fails post-merge validation"
        ) from exc
    # UPDATE predicates mirror the SELECT above for defense-in-depth:
    # ``external_id`` is UNIQUE so this is currently safe under the
    # SELECT alone, but a future schema change relaxing uniqueness
    # could otherwise let this UPDATE silently mutate the wrong row.
    # Keeping the predicates symmetric makes the invariant local to
    # this function rather than load-bearing on schema-level UNIQUE.
    conn.execute(
        """
        UPDATE facets SET metadata = ?
        WHERE external_id = ? AND facet_type = 'automation'
              AND agent_id = ? AND is_deleted = 0
        """,
        (
            json.dumps(new_meta, sort_keys=True, ensure_ascii=False),
            external_id,
            agent_id,
        ),
    )
    audit.write(
        conn,
        op="automation_run_recorded",
        actor="system",
        agent_id=agent_id,
        target_external_id=external_id,
        payload={
            "result_bucket": last_result if last_result in _RESULT_BUCKETS else "other",
            "last_run_at": last_run,
        },
    )
    return True


def get(conn: sqlcipher3.Connection, *, external_id: str) -> Automation | None:
    """Look up one automation by external_id.

    Returns ``None`` when the row does not exist or has been
    soft-deleted. Cross-agent reads are blocked at the MCP boundary;
    this storage-layer helper returns whatever row matches.
    """

    row = conn.execute(
        """
        SELECT id, external_id, agent_id, content, captured_at, metadata,
               embed_status, is_deleted
        FROM facets
        WHERE external_id = ? AND facet_type = 'automation'
        """,
        (external_id,),
    ).fetchone()
    if row is None or bool(row[7]):
        return None
    return _row_to_automation(row)


def list_for_agent(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    runner: str | None = None,
    limit: int = 50,
) -> list[Automation]:
    """List automations owned by ``agent_id``, optionally filtered by runner.

    Ordered by ``captured_at DESC, id DESC`` so the most recent
    registration lands first. ``runner`` filters by the metadata's
    ``runner`` field — caller-side runners use this to narrow the
    registry to "my automations" without scanning the whole list.
    """

    if runner is None:
        rows = conn.execute(
            """
            SELECT id, external_id, agent_id, content, captured_at, metadata,
                   embed_status, is_deleted
            FROM facets
            WHERE agent_id = ? AND facet_type = 'automation' AND is_deleted = 0
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
            WHERE agent_id = ? AND facet_type = 'automation' AND is_deleted = 0
                  AND json_extract(metadata, '$.runner') = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (agent_id, runner, limit),
        ).fetchall()
    return [_row_to_automation(r) for r in rows]


def _row_to_automation(row: tuple[Any, ...]) -> Automation:
    external_id = str(row[1])
    try:
        raw_metadata = json.loads(row[5]) if row[5] else {}
    except json.JSONDecodeError as exc:
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} is not valid JSON"
        ) from exc
    if not isinstance(raw_metadata, dict):
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} is not a JSON object"
        )
    try:
        metadata = validate_metadata(raw_metadata)
    except InvalidAutomationMetadataError as exc:
        raise CorruptAutomationRowError(
            f"stored metadata for automation {external_id!r} drifted from the ADR-0020 contract"
        ) from exc
    return Automation(
        facet_id=int(row[0]),
        external_id=external_id,
        agent_id=int(row[2]),
        content=str(row[3]),
        captured_at=int(row[4]),
        embed_status=str(row[6]),
        metadata=metadata,
    )


def _entry_short_string(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidAutomationMetadataError(
            f"{label} must be a string, got {type(value).__name__}"
        )
    if not value:
        raise InvalidAutomationMetadataError(f"{label} must be non-empty")
    if len(value) > max_chars:
        raise InvalidAutomationMetadataError(f"{label} length {len(value)} exceeds max {max_chars}")
    return value


def _optional_short_string(value: Any, label: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _entry_short_string(value, label, max_chars)


def _optional_iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    text = _entry_short_string(value, "last_run", _MAX_LAST_RUN_CHARS)
    if not _ISO8601_PATTERN.match(text):
        raise InvalidAutomationMetadataError(
            "last_run must be an ISO-8601 timestamp (e.g. '2026-05-02T09:00:00Z')"
        )
    return text


__all__ = [
    "Automation",
    "AutomationError",
    "AutomationMetadata",
    "CorruptAutomationRowError",
    "InvalidAutomationMetadataError",
    "UnknownAutomationError",
    "get",
    "list_for_agent",
    "record_run",
    "register",
    "validate_metadata",
]
