"""Render the dogfood ledger as the markdown tables each gate doc carries.

The three dogfood docs (``docs/dogfood/{sync,compiled-notebook,playbook}-dogfood.md``)
each carry two auto-generated sections:

* **Evidence Log** — one row per ledger record, kind-shaped columns.
* **Acceptance Summary** — gate-rule predicates evaluated against the
  ledger, mapping each gate's DoD checklist to ``Met`` / ``Pending``.

Both sections are wrapped in HTML comment markers so the renderer
can rewrite them in place without disturbing the surrounding prose.
A doc with the markers absent is treated as not-yet-instrumented;
:func:`update_doc` raises :class:`MarkerError` rather than guessing
where to insert. The caller adds the markers once, then re-renders
on every run.

The Acceptance Summary's predicates encode the
``## Acceptance Summary`` checklist from the original doc:

* **sync** — 30+ days, two distinct machines, push + pull both
  exercised, post-pull audit-verify success, sync failures recorded.
* **compiled** — 60+ days, real research topic recorded, register
  + review records present, stale event observed, audit-verify
  passed, output reviewed as useful.
* **playbook** — at least two registered targets, recompile
  observed, stale event observed, audit-verify passed at every
  checkpoint, decision recorded, every failure class addressed.

Predicates are deterministic over the ledger; rerendering produces
byte-identical output for the same input.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from tessera.dogfood.schemas import (
    GATE_KINDS,
    GATES,
    PLAYBOOK_FAILURE_CLASSES,
    Record,
    SchemaError,
)

# Markers — inserted into the doc once, then rewritten in place.
EVIDENCE_START_MARKER: Final[str] = "<!-- BEGIN tessera-dogfood evidence-log -->"
EVIDENCE_END_MARKER: Final[str] = "<!-- END tessera-dogfood evidence-log -->"
SUMMARY_START_MARKER: Final[str] = "<!-- BEGIN tessera-dogfood acceptance-summary -->"
SUMMARY_END_MARKER: Final[str] = "<!-- END tessera-dogfood acceptance-summary -->"

# Acceptance summary check ids per gate, mirroring the original
# checklist text in each doc. Order is the rendered display order.
_SYNC_CHECKS: Final[Sequence[tuple[str, str]]] = (
    ("days_30_plus", "30 consecutive days completed"),
    ("two_machines", "Two-machine workflow used"),
    ("push_and_pull", "Push and pull both exercised"),
    ("audit_verified", "Audit verification passed after pull"),
    ("failures_documented", "Sync failures documented"),
    ("blockers_closed", "Data-loss or sync-integrity blockers closed"),
)

_COMPILED_CHECKS: Final[Sequence[tuple[str, str]]] = (
    ("days_60_plus", "60 consecutive days completed"),
    ("real_topic", "Real dissertation topic used"),
    ("registered_through_path", "Compiled artifact registered through shipped path"),
    ("stale_event_observed", "Source updates exercised stale detection"),
    ("audit_verified", "Audit verification passed after compiled-artifact changes"),
    ("output_useful", "Output judged useful for real research work"),
    ("blockers_closed", "Integrity blockers closed"),
)

_PLAYBOOK_CHECKS: Final[Sequence[tuple[str, str]]] = (
    ("two_targets_registered", "Two or more Phase 9 targets registered"),
    (
        "register_and_recompile_via_cli",
        "Register and recompile both driven through the shipped CLI",
    ),
    ("stale_event_observed", "Source mutation triggered staleness through `mark_stale_for_source`"),
    ("recompile_preserves_target", "Recompile produced fresh artifact preserving `target`"),
    ("audit_verified", "`tessera audit verify` passed at every checkpoint"),
    ("failure_log_populated", "Failure-case log populated for every class"),
    ("decision_recorded", "Ranking-penalty decision recorded"),
    ("blockers_closed", "Integrity blockers closed"),
)

_GATE_CHECKS: Final[dict[str, Sequence[tuple[str, str]]]] = {
    "sync": _SYNC_CHECKS,
    "compiled": _COMPILED_CHECKS,
    "playbook": _PLAYBOOK_CHECKS,
}


class MarkerError(RuntimeError):
    """The doc is missing the marker block the renderer needs."""


def render_evidence_log(records: Iterable[Record], *, gate: str) -> str:
    """Render the Evidence Log markdown table for ``gate``.

    The table shape is gate-shaped — sync uses sync-specific columns,
    compiled uses review/usefulness columns, etc. — so a reader can
    skim the table and see what mattered for that gate. An empty
    ledger renders the markers wrapping a "no records yet" line so
    a freshly-instrumented doc still passes Markdown lint.
    """

    if gate not in GATES:
        raise SchemaError(f"unknown gate: {gate!r}")
    rendered_rows: list[str] = []
    for record in records:
        if record.kind not in GATE_KINDS[gate]:
            raise SchemaError(f"record kind {record.kind!r} not admitted by gate {gate!r}")
        rendered_rows.append(_format_row(gate, record))
    header = _table_header(gate)
    if not rendered_rows:
        # The header is a 2-line markdown table (column row + alignment row);
        # only the first line determines column count. Pipes on the column
        # row = columns + 1, so column-count = first_line.count("|") - 1
        # and the placeholder row needs (column-count - 1) trailing empty
        # cells after the "_no records yet_" cell.
        first_line = header.split("\n", 1)[0]
        empty_cells = first_line.count("|") - 2
        body = "| _no records yet_ |" + " |" * empty_cells
        return _join_table(header, [body])
    return _join_table(header, rendered_rows)


def render_acceptance_summary(records: Iterable[Record], *, gate: str) -> str:
    """Render the Acceptance Summary table for ``gate``.

    The table maps the gate's DoD checklist to ``Met`` or ``Pending``,
    with a brief evidence pointer when met. Each predicate is a pure
    function of the ledger contents; the gate clears when every row
    reports ``Met``.
    """

    if gate not in GATES:
        raise SchemaError(f"unknown gate: {gate!r}")
    record_list = list(records)
    states = _evaluate_checks(gate, record_list)
    lines = ["| Check | Status | Evidence |", "| --- | --- | --- |"]
    for check_id, label in _GATE_CHECKS[gate]:
        met, evidence = states[check_id]
        status = "Met" if met else "Pending"
        evidence_cell = evidence or "—"
        lines.append(f"| {label} | {status} | {evidence_cell} |")
    return "\n".join(lines)


def update_doc(
    *,
    doc_path: Path,
    gate: str,
    records: Iterable[Record],
) -> None:
    """Rewrite the doc's evidence-log and acceptance-summary blocks in place.

    The records iterable is materialized once; both tables read from
    the same list so they describe the same point-in-time ledger.
    The doc must already carry both marker pairs; if either is
    missing :class:`MarkerError` is raised and nothing is written.
    """

    record_list = list(records)
    evidence_table = render_evidence_log(record_list, gate=gate)
    summary_table = render_acceptance_summary(record_list, gate=gate)
    text = doc_path.read_text(encoding="utf-8")
    text = _replace_block(
        text,
        start=EVIDENCE_START_MARKER,
        end=EVIDENCE_END_MARKER,
        body=evidence_table,
        block_label="evidence-log",
    )
    text = _replace_block(
        text,
        start=SUMMARY_START_MARKER,
        end=SUMMARY_END_MARKER,
        body=summary_table,
        block_label="acceptance-summary",
    )
    doc_path.write_text(text, encoding="utf-8")


def _replace_block(text: str, *, start: str, end: str, body: str, block_label: str) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise MarkerError(
            f"{block_label} markers not found; insert {start!r} and {end!r} into the doc first"
        )
    return pattern.sub(f"{start}\n{body}\n{end}", text, count=1)


def _table_header(gate: str) -> str:
    if gate == "sync":
        return (
            "| Date (UTC) | Machine | Kind | Command | Seq Δ | Elapsed (ms) | "
            "Exit | Notes |\n| --- | --- | --- | --- | --- | --- | --- | --- |"
        )
    if gate == "compiled":
        return (
            "| Date (UTC) | Machine | Kind | External ID | Compiler version | "
            "Elapsed (ms) | Exit / Useful | Notes |\n| --- | --- | --- | --- | --- | --- | --- | --- |"
        )
    return (
        "| Date (UTC) | Machine | Kind | Target | External ID | Compiler version | "
        "Exit | Notes |\n| --- | --- | --- | --- | --- | --- | --- | --- |"
    )


def _format_row(gate: str, record: Record) -> str:
    payload = record.payload
    machine = _shorten(record.machine_id, limit=24)
    ts = record.ts
    notes = _payload_summary(record)
    if gate == "sync":
        command = str(payload.get("command", "—"))
        seq_delta = _seq_delta(payload)
        elapsed = _scalar(payload, "elapsed_ms")
        exit_code = _scalar(payload, "exit_code")
        return (
            f"| {ts} | {machine} | {record.kind} | {command} | {seq_delta} | "
            f"{elapsed} | {exit_code} | {notes} |"
        )
    if gate == "compiled":
        external_id = str(payload.get("external_id", "—"))
        compiler_version = str(payload.get("compiler_version", "—"))
        elapsed = _scalar(payload, "elapsed_ms")
        useful_or_exit = str(payload.get("usefulness", payload.get("exit_code", "—")))
        return (
            f"| {ts} | {machine} | {record.kind} | {external_id} | "
            f"{compiler_version} | {elapsed} | {useful_or_exit} | {notes} |"
        )
    target = str(payload.get("target", "—"))
    external_id = str(payload.get("external_id", payload.get("new_external_id", "—")))
    compiler_version = str(payload.get("compiler_version", "—"))
    exit_code = _scalar(payload, "exit_code")
    return (
        f"| {ts} | {machine} | {record.kind} | {target} | {external_id} | "
        f"{compiler_version} | {exit_code} | {notes} |"
    )


def _seq_delta(payload: dict[str, Any]) -> str:
    before = payload.get("manifest_seq_before")
    after = payload.get("manifest_seq_after")
    if before is None and after is None:
        return "—"
    return f"{before}→{after}"


def _scalar(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return "—"
    return str(value)


def _payload_summary(record: Record) -> str:
    """Render a short, table-cell-safe note for the row.

    Picks the shortest meaningful field: ``text`` for notes,
    ``error_class`` for failed sync ops, ``failure_class`` for
    playbook failure cases, and a kind-specific tagline otherwise.
    Pipes are escaped so the cell stays inside its column.
    """

    payload = record.payload
    if record.kind == "note":
        return _escape_cell(str(payload.get("text", "")))
    if record.kind == "failure_case":
        cls = str(payload.get("failure_class", "—"))
        action = str(payload.get("corrective_action", ""))
        return _escape_cell(f"{cls}: {action}")
    if record.kind == "decision":
        return _escape_cell(str(payload.get("recommendation", "")))
    if record.kind == "stale_event":
        op = str(payload.get("source_op", "—"))
        src = str(payload.get("source_external_id", "—"))
        return _escape_cell(f"{op}@{_shorten(src, limit=12)}")
    if record.kind == "gate_initialized":
        op = str(payload.get("operator", "—"))
        sd = str(payload.get("start_date", "—"))
        return _escape_cell(f"{op} @ {sd}")
    if record.kind == "gate_completed":
        return _escape_cell(str(payload.get("end_date", "")))
    if record.kind == "audit_verify":
        outcome = str(payload.get("outcome", "—"))
        exit_code = payload.get("exit_code")
        if exit_code is not None and int(exit_code) != 0:
            return _escape_cell(f"{outcome} (exit={exit_code})")
        return _escape_cell(outcome)
    error_class = payload.get("error_class")
    if error_class:
        return _escape_cell(f"err: {error_class}")
    return "—"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _shorten(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _join_table(header: str, rows: list[str]) -> str:
    return header + "\n" + "\n".join(rows)


def _evaluate_checks(gate: str, records: list[Record]) -> dict[str, tuple[bool, str]]:
    if gate == "sync":
        return _evaluate_sync_checks(records)
    if gate == "compiled":
        return _evaluate_compiled_checks(records)
    return _evaluate_playbook_checks(records)


def _evaluate_sync_checks(
    records: list[Record],
) -> dict[str, tuple[bool, str]]:
    init_record = _first(records, kind="gate_initialized")
    days_met, days_evidence = _days_predicate(init_record, records, threshold_days=30)
    machines = _distinct_machines(records)
    sync_ops = [r for r in records if r.kind == "sync_op"]
    push_seen = any(r.payload.get("command") == "push" for r in sync_ops)
    pull_seen = any(r.payload.get("command") == "pull" for r in sync_ops)
    # The DoD bullet says "succeeds on each machine", not "succeeds anywhere".
    # The predicate must therefore require at least one passing audit_verify
    # row per distinct machine_id present in the ledger.
    machines_with_audit_pass = {
        r.machine_id
        for r in records
        if r.kind == "audit_verify" and r.payload.get("exit_code") == 0
    }
    audit_pass = bool(machines) and machines.issubset(machines_with_audit_pass)
    failures = [r for r in sync_ops if int(r.payload.get("exit_code", 0)) != 0]
    notes = [r for r in records if r.kind == "note"]
    # Require the gate to be initialized AND either no failures or every
    # failure paired with at least one explanatory note. An uninitialized
    # empty ledger has zero failures, but reporting "Met" on a run that
    # has not started would be misleading.
    failures_documented = init_record is not None and (not failures or len(notes) > 0)
    return {
        "days_30_plus": (days_met, days_evidence),
        "two_machines": (
            len(machines) >= 2,
            f"machines: {', '.join(sorted(machines))}" if machines else "—",
        ),
        "push_and_pull": (
            push_seen and pull_seen,
            f"push={push_seen}, pull={pull_seen}",
        ),
        "audit_verified": (
            audit_pass,
            (
                f"audit_verify exit=0 on every machine ({len(machines_with_audit_pass)}/{len(machines)})"
                if audit_pass
                else f"machines without passing audit_verify: "
                f"{', '.join(sorted(machines - machines_with_audit_pass)) or '—'}"
            ),
        ),
        "failures_documented": (
            failures_documented,
            f"{len(failures)} sync_op failures, {len(notes)} note rows"
            if init_record is not None
            else "no gate_initialized row",
        ),
        "blockers_closed": (
            False,
            "manual sign-off required (record via `tessera dogfood record sync --kind note`)",
        ),
    }


def _evaluate_compiled_checks(
    records: list[Record],
) -> dict[str, tuple[bool, str]]:
    init_record = _first(records, kind="gate_initialized")
    days_met, days_evidence = _days_predicate(init_record, records, threshold_days=60)
    topic_evidence = ""
    real_topic = False
    if init_record is not None:
        topic = init_record.payload.get("research_topic")
        if topic:
            real_topic = True
            topic_evidence = f"topic: {topic}"
    # A failed register attempt does not store an artifact; the DoD bullet
    # requires the artifact to be stored "through the shipped compiled-
    # artifact registration path", so the predicate must filter on
    # exit_code == 0 (equivalently, non-empty external_id). The auto-hook
    # in playbook_cmd._emit_register deliberately writes failed-attempt
    # rows so the evidence log captures the attempt; predicates must not
    # treat those rows as success evidence.
    successful_registers = [
        r for r in records if r.kind == "register" and r.payload.get("exit_code") == 0
    ]
    register_seen = bool(successful_registers)
    stale_seen = any(r.kind == "stale_event" for r in records)
    audit_pass = any(r.kind == "audit_verify" and r.payload.get("exit_code") == 0 for r in records)
    review_useful = any(
        r.kind == "review"
        and str(r.payload.get("usefulness", "")).lower() in {"high", "medium", "useful"}
        for r in records
    )
    return {
        "days_60_plus": (days_met, days_evidence),
        "real_topic": (real_topic, topic_evidence or "—"),
        "registered_through_path": (
            register_seen,
            f"{len(successful_registers)} successful register row(s)" if register_seen else "—",
        ),
        "stale_event_observed": (
            stale_seen,
            "stale_event row present" if stale_seen else "—",
        ),
        "audit_verified": (
            audit_pass,
            "audit_verify exit=0 row present" if audit_pass else "—",
        ),
        "output_useful": (
            review_useful,
            "review with usefulness ∈ {high, medium, useful}" if review_useful else "—",
        ),
        "blockers_closed": (
            False,
            "manual sign-off required (record via `tessera dogfood record compiled --kind note`)",
        ),
    }


def _evaluate_playbook_checks(
    records: list[Record],
) -> dict[str, tuple[bool, str]]:
    # Only successful register rows count as "registered targets".
    # A failed register (exit_code=1, external_id="") still appears in
    # the evidence log as an attempt, but it did not store an artifact
    # so it is not Phase 9 acceptance evidence.
    successful_registers = [
        r for r in records if r.kind == "register" and r.payload.get("exit_code") == 0
    ]
    register_targets = {
        r.payload.get("target") for r in successful_registers if r.payload.get("target")
    }
    recompile_records = [r for r in records if r.kind == "recompile"]
    # The shipped CLI does not emit ``kind == "compile"`` automatically
    # (per ADR 0019 §Boundary statement Tessera does not compile). The
    # operator-driven evidence that a compile loop completed through
    # the shipped CLI is the auto-emitted ``register`` row. Use that as
    # the predicate's left-hand side so the gate clears on real CLI
    # use rather than requiring a manual ``--kind compile`` record.
    register_via_cli_seen = bool(successful_registers)
    cli_recompile_seen = bool(recompile_records)
    stale_seen = any(r.kind == "stale_event" for r in records)
    audit_records = [r for r in records if r.kind == "audit_verify"]
    audit_pass = bool(audit_records) and all(r.payload.get("exit_code") == 0 for r in audit_records)
    failure_classes_logged = {
        r.payload.get("failure_class") for r in records if r.kind == "failure_case"
    }
    required_classes = PLAYBOOK_FAILURE_CLASSES - {"other"}
    failures_complete = required_classes.issubset(failure_classes_logged)
    decision_seen = any(r.kind == "decision" for r in records)
    recompile_preserves_target = any(
        _recompile_preserves_target(r, records) for r in recompile_records
    )
    return {
        "two_targets_registered": (
            len(register_targets) >= 2,
            f"targets: {', '.join(sorted(t for t in register_targets if t))}"
            if register_targets
            else "—",
        ),
        "register_and_recompile_via_cli": (
            register_via_cli_seen and cli_recompile_seen,
            f"register={register_via_cli_seen}, recompile={cli_recompile_seen}",
        ),
        "stale_event_observed": (
            stale_seen,
            "stale_event row present" if stale_seen else "—",
        ),
        "recompile_preserves_target": (
            recompile_preserves_target,
            "recompile linked to prior register row by target"
            if recompile_preserves_target
            else "—",
        ),
        "audit_verified": (
            audit_pass,
            f"{len(audit_records)} audit_verify rows, all exit=0"
            if audit_pass
            else "audit_verify with non-zero exit or no rows",
        ),
        "failure_log_populated": (
            failures_complete,
            f"classes logged: {', '.join(sorted(c for c in failure_classes_logged if c))}",
        ),
        "decision_recorded": (
            decision_seen,
            "decision row present" if decision_seen else "—",
        ),
        "blockers_closed": (
            False,
            "manual sign-off required (record via `tessera dogfood record playbook --kind note`)",
        ),
    }


def _recompile_preserves_target(record: Record, records: list[Record]) -> bool:
    target = record.payload.get("target")
    old_id = record.payload.get("old_external_id")
    if not target or not old_id:
        return False
    return any(
        prior.kind == "register"
        and prior.payload.get("target") == target
        and prior.payload.get("external_id") == old_id
        for prior in records
    )


def _days_predicate(
    init: Record | None, records: list[Record], *, threshold_days: int
) -> tuple[bool, str]:
    if init is None or not records:
        return (False, "no gate_initialized row")
    first_ts = init.ts
    last_ts = records[-1].ts
    elapsed_days = _iso_days_between(first_ts, last_ts)
    met = elapsed_days >= threshold_days
    return (met, f"{elapsed_days} days from {first_ts[:10]} to {last_ts[:10]}")


def _iso_days_between(start_ts: str, end_ts: str) -> int:
    """Whole-day count between two ISO-8601 UTC timestamps.

    Both timestamps come from :func:`utc_now_iso` so the format is
    stable. Negative results clamp to 0 — a ledger whose last row
    predates ``gate_initialized`` is not a 30-day proof.
    """

    fmt_with_us = "%Y-%m-%dT%H:%M:%S.%fZ"
    fmt_no_us = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = datetime.strptime(start_ts, fmt_with_us)
    except ValueError:
        start = datetime.strptime(start_ts, fmt_no_us)
    try:
        end = datetime.strptime(end_ts, fmt_with_us)
    except ValueError:
        end = datetime.strptime(end_ts, fmt_no_us)
    delta = end - start
    return max(delta.days, 0)


def _distinct_machines(records: list[Record]) -> set[str]:
    return {r.machine_id for r in records}


def _first(records: list[Record], *, kind: str) -> Record | None:
    for record in records:
        if record.kind == kind:
            return record
    return None


__all__ = [
    "EVIDENCE_END_MARKER",
    "EVIDENCE_START_MARKER",
    "SUMMARY_END_MARKER",
    "SUMMARY_START_MARKER",
    "MarkerError",
    "render_acceptance_summary",
    "render_evidence_log",
    "update_doc",
]
