"""Dogfood evidence record schemas.

The ledger is JSONL: one record per line, fields fixed at write time
under ``SCHEMA_VERSION``. A record discriminates on ``(gate, kind)``:

* ``gate`` ∈ ``{"sync", "compiled", "playbook"}`` — which dogfood doc
  the row belongs to.
* ``kind`` — the action kind. Every gate admits ``gate_initialized``,
  ``audit_verify``, ``note``, ``gate_completed``; the rest are
  gate-specific (``sync_op`` for the sync gate; ``compile`` /
  ``register`` / ``review`` / ``stale_event`` for the compiled-notebook
  gate; the playbook gate adds ``recompile`` / ``failure_case`` /
  ``decision``).

The frame fields (``schema_version``, ``gate``, ``kind``, ``ts``,
``machine_id``, ``tessera_version``) are uniform; the kind-specific
data lives in ``payload``. ``validate_payload`` enforces the required
keys per ``(gate, kind)``; unknown keys are tolerated so a future
schema bump can add fields without invalidating older rows.

Validation fails loud. The renderer and the CLI both surface
``SchemaError`` with the row index when a stored ledger drifts
from the schema.
"""

from __future__ import annotations

import json
import platform
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from tessera import __version__ as TESSERA_VERSION

SCHEMA_VERSION: Final[int] = 1

GATES: Final[frozenset[str]] = frozenset({"sync", "compiled", "playbook"})

_COMMON_KINDS: Final[frozenset[str]] = frozenset(
    {"gate_initialized", "audit_verify", "note", "gate_completed"}
)

# Gate → admitted kinds. The intersection with ``_COMMON_KINDS`` is
# the universal floor every gate carries; the gate-specific kinds
# below extend it.
GATE_KINDS: Final[dict[str, frozenset[str]]] = {
    "sync": _COMMON_KINDS | frozenset({"sync_op"}),
    "compiled": _COMMON_KINDS | frozenset({"compile", "register", "review", "stale_event"}),
    "playbook": _COMMON_KINDS
    | frozenset(
        {
            "compile",
            "register",
            "stale_event",
            "recompile",
            "failure_case",
            "decision",
        }
    ),
}

# (gate, kind) → required payload keys. Every required key must be
# present for ``validate_payload`` to accept the row. Optional keys
# are documented inline above each entry; unknown keys are tolerated
# so forward-compatible additions do not invalidate old rows.
_REQUIRED_PAYLOAD_KEYS: Final[dict[tuple[str, str], frozenset[str]]] = {
    # Common kinds — every gate uses the same payload shape.
    # ``operator`` + ``start_date`` are pinned at gate-initialization
    # so v0.5 GA reviewers can read who started the run and when.
    # Gate-specific extras (machines, sync_backend, research topic)
    # ride along as optional payload keys.
    ("sync", "gate_initialized"): frozenset({"operator", "start_date"}),
    ("compiled", "gate_initialized"): frozenset({"operator", "start_date"}),
    ("playbook", "gate_initialized"): frozenset({"operator", "start_date"}),
    # ``audit_verify`` — exit code is the load-bearing signal. Total
    # rows + head id are optional context the verifier already prints.
    ("sync", "audit_verify"): frozenset({"exit_code"}),
    ("compiled", "audit_verify"): frozenset({"exit_code"}),
    ("playbook", "audit_verify"): frozenset({"exit_code"}),
    # ``note`` — free-form text, used for context the structured
    # kinds do not carry. ``text`` is mandatory; nothing else.
    ("sync", "note"): frozenset({"text"}),
    ("compiled", "note"): frozenset({"text"}),
    ("playbook", "note"): frozenset({"text"}),
    # ``gate_completed`` — the closing row. Operator records the end
    # date and a short summary.
    ("sync", "gate_completed"): frozenset({"end_date"}),
    ("compiled", "gate_completed"): frozenset({"end_date"}),
    ("playbook", "gate_completed"): frozenset({"end_date"}),
    # Sync-specific kinds.
    # ``command`` ∈ {push, pull}; manifest sequence numbers are -1 when
    # the operation never reached the manifest layer (e.g., setup
    # failure). ``error_class`` is the qualname of the raised exception
    # when ``exit_code != 0``; null on success.
    ("sync", "sync_op"): frozenset(
        {
            "command",
            "exit_code",
            "elapsed_ms",
        }
    ),
    # Compiled-notebook-specific kinds.
    ("compiled", "compile"): frozenset({"compiler_version", "elapsed_ms"}),
    ("compiled", "register"): frozenset({"external_id", "compiler_version"}),
    ("compiled", "review"): frozenset({"usefulness"}),
    ("compiled", "stale_event"): frozenset({"source_external_id", "source_op"}),
    # Playbook-specific kinds.
    ("playbook", "compile"): frozenset({"target", "compiler_version", "elapsed_ms"}),
    ("playbook", "register"): frozenset({"target", "external_id", "compiler_version"}),
    ("playbook", "stale_event"): frozenset(
        {"source_external_id", "source_op", "stale_count_after"}
    ),
    ("playbook", "recompile"): frozenset(
        {"target", "old_external_id", "new_external_id", "compiler_version"}
    ),
    ("playbook", "failure_case"): frozenset({"failure_class", "target", "corrective_action"}),
    ("playbook", "decision"): frozenset({"decision_id", "recommendation"}),
}

# Failure classes the playbook gate's ## Failure cases section
# enumerates verbatim. Anything else lands as ``"other"``.
PLAYBOOK_FAILURE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "stale_artifact_trusted_accidentally",
        "source_missing_from_compiled_output",
        "eval_passed_but_answer_was_weak",
        "artifact_too_lossy_for_exploratory_use",
        "other",
    }
)

# ISO-8601 UTC timestamp pattern: ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``.
_TS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class SchemaError(ValueError):
    """A ledger row violates the dogfood schema."""


def utc_now_iso() -> str:
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Mirrors ``vault.canonical_json``'s timestamp shape so dogfood rows
    compare lexicographically under the same convention the audit
    chain uses.
    """

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def machine_id() -> str:
    """Return a stable per-host identifier for the ledger.

    Hostname plus architecture (``"laptop.local|arm64"``) so a record
    survives the multi-machine sync gate's two-host requirement
    without colliding when ``hostname`` happens to match.
    Best-effort: if either lookup fails the record falls back to
    ``"unknown"`` rather than raising, since dogfood is instrumentation
    and must not break the user's command.
    """

    try:
        host = socket.gethostname() or "unknown"
    except OSError:
        host = "unknown"
    arch = platform.machine() or "unknown"
    return f"{host}|{arch}"


def require_kind(gate: str, kind: str) -> None:
    """Raise :class:`SchemaError` when ``kind`` is not admitted by ``gate``."""

    if gate not in GATES:
        raise SchemaError(f"unknown gate: {gate!r}")
    if kind not in GATE_KINDS[gate]:
        admitted = ", ".join(sorted(GATE_KINDS[gate]))
        raise SchemaError(f"kind {kind!r} not admitted by gate {gate!r}; admitted: {admitted}")


def validate_payload(gate: str, kind: str, payload: dict[str, Any]) -> None:
    """Enforce required-key presence for ``(gate, kind)``.

    Optional keys are tolerated; unknown keys are tolerated.
    Forward-compat: a future ``SCHEMA_VERSION`` bump can add required
    keys, and old rows (which lack them) will fail validation only
    when they are read under the new schema.
    """

    require_kind(gate, kind)
    required = _REQUIRED_PAYLOAD_KEYS.get((gate, kind), frozenset())
    missing = sorted(required - payload.keys())
    if missing:
        raise SchemaError(f"({gate}, {kind}) payload missing required keys: {missing}")


@dataclass(frozen=True, slots=True)
class Record:
    """One row of the dogfood ledger.

    The frame fields are stable across kinds; the kind-specific data
    lives in ``payload``. ``Record.make`` fills in ``schema_version``,
    ``ts``, ``machine_id``, and ``tessera_version`` so call sites only
    pass the gate / kind / payload they care about.
    """

    schema_version: int
    gate: str
    kind: str
    ts: str
    machine_id: str
    tessera_version: str
    payload: dict[str, Any]

    @classmethod
    def make(
        cls,
        *,
        gate: str,
        kind: str,
        payload: dict[str, Any],
        ts: str | None = None,
        host: str | None = None,
        version: str | None = None,
    ) -> Record:
        """Build a record with frame fields auto-populated."""

        validate_payload(gate, kind, payload)
        record = cls(
            schema_version=SCHEMA_VERSION,
            gate=gate,
            kind=kind,
            ts=ts or utc_now_iso(),
            machine_id=host or machine_id(),
            tessera_version=version or TESSERA_VERSION,
            payload=dict(payload),
        )
        record.validate()
        return record

    def validate(self) -> None:
        """Enforce the frame contract on a fully-constructed record.

        Run on construction (via :meth:`make`) and on read (via
        :meth:`from_jsonl_line`) so rows that round-trip stay honest
        across the JSONL boundary.
        """

        if self.schema_version != SCHEMA_VERSION:
            raise SchemaError(
                f"unsupported schema_version {self.schema_version}; "
                f"this build understands {SCHEMA_VERSION}"
            )
        if self.gate not in GATES:
            raise SchemaError(f"unknown gate: {self.gate!r}")
        if not _TS_PATTERN.match(self.ts):
            raise SchemaError(f"ts {self.ts!r} does not match YYYY-MM-DDTHH:MM:SS[.ffffff]Z")
        if not self.machine_id:
            raise SchemaError("machine_id is empty")
        if not self.tessera_version:
            raise SchemaError("tessera_version is empty")
        validate_payload(self.gate, self.kind, self.payload)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (frame fields + payload)."""

        return {
            "schema_version": self.schema_version,
            "gate": self.gate,
            "kind": self.kind,
            "ts": self.ts,
            "machine_id": self.machine_id,
            "tessera_version": self.tessera_version,
            "payload": self.payload,
        }

    def to_jsonl_line(self) -> str:
        """Serialize as one JSON line (no trailing newline).

        Stable key order so a record byte-compares equal across writes
        of the same data — useful for the renderer's idempotency guard
        and for diffing checked-in rendered tables.
        """

        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_jsonl_line(cls, line: str) -> Record:
        """Parse one JSONL line; raise :class:`SchemaError` on drift."""

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise SchemaError(f"expected JSON object, got {type(obj).__name__}")
        try:
            record = cls(
                schema_version=int(obj["schema_version"]),
                gate=str(obj["gate"]),
                kind=str(obj["kind"]),
                ts=str(obj["ts"]),
                machine_id=str(obj["machine_id"]),
                tessera_version=str(obj["tessera_version"]),
                payload=dict(obj["payload"]),
            )
        except KeyError as exc:
            raise SchemaError(f"missing frame field: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"malformed frame: {exc}") from exc
        record.validate()
        return record


__all__ = [
    "GATES",
    "GATE_KINDS",
    "PLAYBOOK_FAILURE_CLASSES",
    "SCHEMA_VERSION",
    "Record",
    "SchemaError",
    "machine_id",
    "require_kind",
    "utc_now_iso",
    "validate_payload",
]
