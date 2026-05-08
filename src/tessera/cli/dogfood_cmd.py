"""``tessera dogfood`` — manage v0.5 GA dogfood evidence ledgers.

Four subcommands, gate-scoped:

* ``init <gate>`` — open a gate. Writes a ``gate_initialized`` row
  pinning operator + start-date. Refuses when the gate is already
  active so two operators cannot fight over the same ledger.
* ``record <gate> --kind KIND`` — append a typed event. The kind
  must belong to the gate's allowlist; payload comes from
  ``--payload-json`` or repeated ``--field key=value`` flags.
* ``render <gate>`` — regenerate the gate doc's Evidence Log and
  Acceptance Summary tables from the ledger between markers. Run
  with ``--no-write`` to print without rewriting the doc.
* ``status [<gate>]`` — show ledger state (active/completed/row count
  per gate) and the last record's timestamp; without a gate prints
  the summary for all three.

Auto-emission from existing CLI commands (``tessera audit verify``,
``tessera sync push|pull``) calls ``auto_record`` directly; the CLI
here is the manual side-channel for notes, decisions, failure cases,
and the lifecycle bookend rows.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from tessera.cli._common import CliError, fail
from tessera.cli._ui import info, raw, report_table, success, warn
from tessera.dogfood.ledger import (
    DEFAULT_LEDGER_DIR,
    DogfoodEmissionError,
    Ledger,
    LedgerCorruptionError,
    auto_record,
    is_disabled,
    ledger_dir,
    ledger_path,
)
from tessera.dogfood.render import (
    EVIDENCE_END_MARKER,
    EVIDENCE_START_MARKER,
    SUMMARY_END_MARKER,
    SUMMARY_START_MARKER,
    MarkerError,
    render_acceptance_summary,
    render_evidence_log,
    update_doc,
)
from tessera.dogfood.schemas import (
    GATE_KINDS,
    GATES,
    PLAYBOOK_FAILURE_CLASSES,
    Record,
    SchemaError,
)

_HELP_DESCRIPTION: Final[str] = (
    "Manage v0.5 GA dogfood evidence ledgers.\n\n"
    "One JSONL ledger per gate (sync, compiled, playbook) under\n"
    "$TESSERA_DOGFOOD_DIR (default: ~/.tessera/dogfood/). Records auto-\n"
    "emit from `tessera audit verify` and `tessera sync push|pull` when\n"
    "a gate is active; this CLI handles manual lifecycle events,\n"
    "structured notes, decisions, and the doc renderer.\n\n"
    "Subcommands: init | record | render | status"
)

_DEFAULT_DOC_PATHS: Final[dict[str, Path]] = {
    "sync": Path("docs/dogfood/sync-dogfood.md"),
    "compiled": Path("docs/dogfood/compiled-notebook-dogfood.md"),
    "playbook": Path("docs/dogfood/playbook-dogfood.md"),
}


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``tessera dogfood`` command tree on ``subparsers``."""

    parser = subparsers.add_parser(
        "dogfood",
        help="manage v0.5 GA dogfood evidence ledgers",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="dogfood_command")

    init_p = sub.add_parser(
        "init",
        help="open a dogfood gate (writes a gate_initialized row)",
    )
    init_p.add_argument("gate", choices=sorted(GATES))
    init_p.add_argument("--operator", required=True, help="named operator running the gate")
    init_p.add_argument(
        "--start-date",
        required=True,
        help="ISO calendar start date (YYYY-MM-DD); the day count is measured from here",
    )
    init_p.add_argument(
        "--field",
        action="append",
        default=None,
        dest="fields",
        help=(
            "extra payload key=value pairs (repeatable). "
            "Values are auto-coerced to int/float/bool when they look the part."
        ),
    )
    init_p.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="override the ledger directory (default: $TESSERA_DOGFOOD_DIR or ~/.tessera/dogfood/)",
    )
    init_p.set_defaults(handler=_cmd_init)

    record_p = sub.add_parser(
        "record",
        help="append a typed record to a gate's ledger",
    )
    record_p.add_argument("gate", choices=sorted(GATES))
    record_p.add_argument(
        "--kind",
        required=True,
        help="record kind; must be admitted by the gate (see `tessera dogfood status <gate>`)",
    )
    record_p.add_argument(
        "--field",
        action="append",
        default=None,
        dest="fields",
        help="payload key=value pairs (repeatable)",
    )
    record_p.add_argument(
        "--payload-json",
        default=None,
        help="payload as a JSON object string; merges with --field (--field wins on collision)",
    )
    record_p.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="override the ledger directory",
    )
    record_p.set_defaults(handler=_cmd_record)

    render_p = sub.add_parser(
        "render",
        help="regenerate the gate doc's evidence + summary tables",
    )
    render_p.add_argument("gate", choices=sorted(GATES))
    render_p.add_argument(
        "--doc",
        type=Path,
        default=None,
        help=(
            "path to the gate doc; default: docs/dogfood/<gate>-dogfood.md "
            "(playbook -> playbook-dogfood.md, compiled -> compiled-notebook-dogfood.md)"
        ),
    )
    render_p.add_argument(
        "--no-write",
        action="store_true",
        help="print the rendered tables instead of rewriting the doc",
    )
    render_p.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="override the ledger directory",
    )
    render_p.set_defaults(handler=_cmd_render)

    status_p = sub.add_parser(
        "status",
        help="show gate lifecycle state and ledger location",
    )
    status_p.add_argument(
        "gate",
        nargs="?",
        choices=sorted(GATES),
        default=None,
        help="gate to report; omit to see all three",
    )
    status_p.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="override the ledger directory",
    )
    status_p.set_defaults(handler=_cmd_status)

    parser.set_defaults(handler=_print_help_when_no_subcommand(parser))


def _print_help_when_no_subcommand(
    parser: argparse.ArgumentParser,
) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _handler


def _cmd_init(args: argparse.Namespace) -> int:
    if is_disabled():
        return fail("TESSERA_DOGFOOD_DISABLE=1 is set; unset it before initializing a gate")
    gate: str = args.gate
    base_dir: Path | None = args.ledger_dir
    ledger = Ledger(gate, base_dir=base_dir)
    try:
        state = ledger.state()
    except LedgerCorruptionError as exc:
        return fail(str(exc))
    if state.active:
        return fail(
            f"gate {gate!r} is already active at {ledger.path}; "
            "complete it (`tessera dogfood record {gate} --kind gate_completed`) "
            "or pick a different ledger directory"
        )
    try:
        payload = _build_payload(
            args, base={"operator": args.operator, "start_date": args.start_date}
        )
        record = Record.make(gate=gate, kind="gate_initialized", payload=payload)
        ledger.append(record)
    except (CliError, SchemaError) as exc:
        return fail(str(exc))
    success(
        f"gate {gate!r} initialized at {ledger.path} (operator={args.operator}, "
        f"start_date={args.start_date})"
    )
    info(f"ledger directory: {ledger_dir(base_dir=base_dir)}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    if is_disabled():
        return fail("TESSERA_DOGFOOD_DISABLE=1 is set; unset it before recording")
    gate: str = args.gate
    kind: str = args.kind
    base_dir: Path | None = args.ledger_dir
    if kind not in GATE_KINDS[gate]:
        admitted = ", ".join(sorted(GATE_KINDS[gate]))
        return fail(f"kind {kind!r} not admitted by gate {gate!r}; admitted: {admitted}")
    ledger = Ledger(gate, base_dir=base_dir)
    try:
        state = ledger.state()
    except LedgerCorruptionError as exc:
        return fail(str(exc))
    if not state.initialized and kind != "gate_initialized":
        return fail(f"gate {gate!r} is not initialized; run `tessera dogfood init {gate}` first")
    if state.completed and kind != "note":
        return fail(f"gate {gate!r} is completed; only `--kind note` is accepted afterwards")
    try:
        payload = _build_payload(args, base={})
        if kind == "failure_case" and gate == "playbook":
            cls = payload.get("failure_class")
            if cls not in PLAYBOOK_FAILURE_CLASSES:
                return fail(
                    f"failure_class {cls!r} not in allowlist; "
                    f"choose from {sorted(PLAYBOOK_FAILURE_CLASSES)}"
                )
        record = Record.make(gate=gate, kind=kind, payload=payload)
        ledger.append(record)
    except (CliError, SchemaError) as exc:
        return fail(str(exc))
    success(f"recorded {kind!r} on gate {gate!r} at {record.ts}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    gate: str = args.gate
    ledger = Ledger(gate, base_dir=args.ledger_dir)
    try:
        records = list(ledger.iter_records())
    except LedgerCorruptionError as exc:
        return fail(str(exc))
    if args.no_write:
        info(f"# Evidence Log ({gate})")
        raw(render_evidence_log(records, gate=gate))
        info(f"# Acceptance Summary ({gate})")
        raw(render_acceptance_summary(records, gate=gate))
        return 0
    doc_path: Path = args.doc or _DEFAULT_DOC_PATHS[gate]
    if not doc_path.exists():
        return fail(f"doc not found: {doc_path}")
    try:
        update_doc(doc_path=doc_path, gate=gate, records=records)
    except MarkerError as exc:
        return fail(
            f"{exc}; insert the marker pairs into {doc_path} before re-rendering. "
            f"Markers: {EVIDENCE_START_MARKER!r}/{EVIDENCE_END_MARKER!r} and "
            f"{SUMMARY_START_MARKER!r}/{SUMMARY_END_MARKER!r}"
        )
    success(f"rendered {len(records)} record(s) into {doc_path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    base_dir: Path | None = args.ledger_dir
    gates = [args.gate] if args.gate else sorted(GATES)
    table = report_table(
        f"Dogfood gates ({ledger_dir(base_dir=base_dir)})",
        ["Gate", "State", "Rows", "Last record", "Path"],
    )
    for gate in gates:
        ledger = Ledger(gate, base_dir=base_dir)
        try:
            state = ledger.state()
            latest = ledger.latest()
        except LedgerCorruptionError as exc:
            warn(f"{gate}: {exc}")
            table.add_row(gate, "CORRUPT", "?", "?", str(ledger.path))
            continue
        state_label = (
            "completed" if state.completed else ("active" if state.active else "uninitialized")
        )
        last_label = f"{latest.ts} ({latest.kind})" if latest is not None else "—"
        table.add_row(gate, state_label, str(state.rows), last_label, str(ledger.path))
    from tessera.cli._ui import console

    console.print(table)
    if is_disabled():
        warn("TESSERA_DOGFOOD_DISABLE=1 is set; auto-emission from other commands is OFF")
    return 0


def _build_payload(args: argparse.Namespace, *, base: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(base)
    json_blob: str | None = getattr(args, "payload_json", None)
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise CliError(f"--payload-json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CliError("--payload-json must decode to a JSON object")
        payload.update(parsed)
    fields: list[str] | None = getattr(args, "fields", None)
    if fields:
        for entry in fields:
            key, value = _parse_field(entry)
            payload[key] = value
    return payload


def _parse_field(entry: str) -> tuple[str, Any]:
    if "=" not in entry:
        raise CliError(f"--field must be key=value, got {entry!r}")
    key, _, raw_value = entry.partition("=")
    key = key.strip()
    if not key:
        raise CliError(f"--field key empty in {entry!r}")
    return key, _coerce(raw_value)


def _coerce(raw_value: str) -> Any:
    """Coerce a CLI string to int/float/bool/null/str.

    Conservative: only obvious literal shapes are coerced. Anything
    that does not match keeps its string form so the operator can
    pass arbitrary text without escaping.
    """

    stripped = raw_value.strip()
    if stripped == "":
        return ""
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if stripped.lower() in {"null", "none"}:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    if (stripped.startswith("[") and stripped.endswith("]")) or (
        stripped.startswith("{") and stripped.endswith("}")
    ):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


__all__ = [
    "DEFAULT_LEDGER_DIR",
    "DogfoodEmissionError",
    "auto_record",
    "ledger_path",
    "register",
]
