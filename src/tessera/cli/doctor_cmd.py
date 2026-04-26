"""``tessera doctor`` — health-check matrix + diagnostic bundle."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from tessera import __version__ as TESSERA_VERSION
from tessera.adapters import models_registry
from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._ui import (
    EMOJI,
    console,
    info,
    report_table,
    status,
    status_cell,
    success,
    warn,
)
from tessera.daemon.config import resolve_config
from tessera.daemon.doctor import DoctorStatus, run_all
from tessera.observability.bundle import BundleSpec, build_bundle, review_instructions
from tessera.observability.events import EventLog
from tessera.observability.scrub import ScrubberViolationError


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("doctor", help="run health checks")
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db (vault checks skip when missing)",
    )
    parser.add_argument("--passphrase", default=None)
    parser.add_argument(
        "--collect",
        metavar="NAME",
        default=None,
        help="produce a diagnostic bundle under --out-dir (requires a vault)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="directory for the --collect tarball; defaults to the current directory",
    )
    parser.set_defaults(handler=_cmd_doctor)


def _cmd_doctor(args: argparse.Namespace) -> int:
    if args.collect is not None:
        return _cmd_collect(args)
    try:
        vault_path = resolve_vault_path(args.vault)
    except CliError as exc:
        return fail(str(exc))
    config = resolve_config(vault_path=vault_path)
    with status("running health checks", emoji=EMOJI["doctor"]):
        if not vault_path.exists():
            # No vault file yet (fresh install). Vault-dependent checks
            # downgrade to WARN; run without opening.
            report = asyncio.run(run_all(config))
        else:
            try:
                passphrase = resolve_passphrase(args.passphrase)
            except CliError as exc:
                return fail(str(exc))
            with open_vault(vault_path, passphrase) as vc:
                report = asyncio.run(run_all(config, conn=vc.connection))
    table = report_table("doctor report", ["check", "status", "detail"], emoji=EMOJI["doctor"])
    for result in report.results:
        table.add_row(result.name, status_cell(result.status.value), result.detail)
    console.print(table)
    verdict = report.verdict
    if verdict is DoctorStatus.OK:
        success("all checks green", emoji=EMOJI["ok"])
        return 0
    if verdict is DoctorStatus.WARN:
        warn("one or more checks reported WARN; review the table above")
        return 0
    # ERROR: non-zero exit so CI / scripts catch it.
    return fail("one or more checks reported ERROR; see table above")


def _cmd_collect(args: argparse.Namespace) -> int:
    """Produce a diagnostic bundle tarball for the given vault.

    The bundle is built locally; Tessera never uploads it. The CLI
    prints an explicit review-before-share instruction after the
    tarball lands so the operator knows to open it first.
    """

    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    if not vault_path.exists():
        return fail(f"--collect requires an initialised vault; nothing at {vault_path}")
    out_dir = args.out_dir or Path.cwd()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"tessera-bundle-{args.collect}-{stamp}"
    config = resolve_config(vault_path=vault_path)
    event_log: EventLog | None = None
    try:
        event_log = EventLog.open(config.events_db_path)
    except Exception as exc:
        # Missing or unreadable events.db is not a showstopper — the
        # bundle just reports an empty recent_events file. Surface a
        # one-liner so the operator understands the reduced content.
        warn(f"events.db unavailable: {type(exc).__name__}; recent_events will be empty")
    try:
        with (
            status("collecting diagnostic bundle", emoji=EMOJI["export"]),
            open_vault(vault_path, passphrase) as vc,
        ):
            active_models = tuple(
                m.name for m in models_registry.list_models(vc.connection) if m.is_active
            )
            spec = BundleSpec(
                vault_conn=vc.connection,
                vault_path=vault_path,
                event_log=event_log,
                tessera_version=TESSERA_VERSION,
                active_models=active_models,
            )
            try:
                result = build_bundle(spec, out_dir=out_dir, name=name)
            except ScrubberViolationError as exc:
                return fail(f"bundle refused by scrubber:\n{exc}")
    finally:
        if event_log is not None:
            event_log.close()
    success(f"bundle written: {result.tarball_path}", emoji=EMOJI["export"])
    info(review_instructions(result))
    return 0
