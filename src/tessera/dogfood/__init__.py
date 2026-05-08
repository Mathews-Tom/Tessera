"""Dogfood evidence ledger for v0.5 GA gates.

Three v0.5 dogfood gates require longitudinal real-world evidence
that test suites cannot supply:

* `docs/dogfood/sync-dogfood.md` — 30+ days of multi-machine sync.
* `docs/dogfood/compiled-notebook-dogfood.md` — 60+ days of write-time
  compilation against a real research topic.
* `docs/dogfood/playbook-dogfood.md` — task-shaped Playbook compile /
  recompile / staleness loops with a populated failure-case log.

This package is the structured evidence channel those docs depend on.
A small JSONL ledger per gate lives under
``$TESSERA_HOME/dogfood/<gate>.jsonl`` (default
``~/.tessera/dogfood/<gate>.jsonl``) and accumulates one append-only
row per real action — gate initialization, sync push / pull, audit
verify, compile / register, staleness flip, recompile, decision,
failure case, note. The CLI (``tessera dogfood``) drives manual entries
and rendering; the existing CLI surfaces (``tessera audit verify``,
``tessera sync push|pull``, etc.) auto-emit rows when a gate is active.

The Markdown docs stay the human-readable narrative; the renderer
(``tessera dogfood render``) regenerates the doc's Evidence Log and
Acceptance Summary tables from the ledger between fenced markers so
the published evidence comes from the same source the auto-hooks
write to. Synthetic rows are not allowed — every row carries a real
machine_id and a real timestamp, and the gate-initialized row pins
the operator + start date that v0.5 GA review reads.
"""

from __future__ import annotations

from tessera.dogfood.ledger import (
    DEFAULT_LEDGER_DIR,
    DogfoodEmissionError,
    Ledger,
    LedgerCorruptionError,
    auto_record,
    is_disabled,
    ledger_path,
)
from tessera.dogfood.render import (
    EVIDENCE_END_MARKER,
    EVIDENCE_START_MARKER,
    SUMMARY_END_MARKER,
    SUMMARY_START_MARKER,
    render_acceptance_summary,
    render_evidence_log,
    update_doc,
)
from tessera.dogfood.schemas import (
    GATE_KINDS,
    GATES,
    SCHEMA_VERSION,
    Record,
    SchemaError,
    machine_id,
    require_kind,
    utc_now_iso,
    validate_payload,
)

__all__ = [
    "DEFAULT_LEDGER_DIR",
    "EVIDENCE_END_MARKER",
    "EVIDENCE_START_MARKER",
    "GATES",
    "GATE_KINDS",
    "SCHEMA_VERSION",
    "SUMMARY_END_MARKER",
    "SUMMARY_START_MARKER",
    "DogfoodEmissionError",
    "Ledger",
    "LedgerCorruptionError",
    "Record",
    "SchemaError",
    "auto_record",
    "is_disabled",
    "ledger_path",
    "machine_id",
    "render_acceptance_summary",
    "render_evidence_log",
    "require_kind",
    "update_doc",
    "utc_now_iso",
    "validate_payload",
]
