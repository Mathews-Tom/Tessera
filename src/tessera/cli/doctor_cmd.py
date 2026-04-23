"""``tessera doctor`` — health-check matrix + diagnostic bundle."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from tessera import __version__ as TESSERA_VERSION
from tessera.adapters import models_registry
from tessera.cli._common import CliError, fail, open_vault, resolve_passphrase
from tessera.daemon.config import resolve_config
from tessera.daemon.doctor import DoctorStatus, run_all
from tessera.observability.bundle import BundleSpec, build_bundle, review_instructions
from tessera.observability.events import EventLog
from tessera.observability.scrub import ScrubberViolationError


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("doctor", help="run health checks")
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--passphrase", default=None)
    parser.add_argument(
        "--collect",
        metavar="NAME",
        default=None,
        help="produce a diagnostic bundle under --out-dir (requires --vault)",
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
    config = resolve_config(vault_path=args.vault)
    if args.vault is None:
        # Vault-dependent checks downgrade to WARN; run without opening.
        report = asyncio.run(run_all(config))
    else:
        try:
            passphrase = resolve_passphrase(args.passphrase)
        except CliError as exc:
            return fail(str(exc))
        with open_vault(args.vault, passphrase) as vc:
            report = asyncio.run(run_all(config, conn=vc.connection))
    symbol = {
        DoctorStatus.OK: "[OK]  ",
        DoctorStatus.WARN: "[WARN]",
        DoctorStatus.ERROR: "[FAIL]",
    }
    for result in report.results:
        print(f"{symbol[result.status]} {result.name:16} {result.detail}")
    print(f"\nverdict: {report.verdict.value}")
    if report.verdict is DoctorStatus.ERROR:
        return 1
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    """Produce a diagnostic bundle tarball for the given vault.

    The bundle is built locally; Tessera never uploads it. The CLI
    prints an explicit review-before-share instruction after the
    tarball lands so the operator knows to open it first.
    """

    if args.vault is None:
        return fail("--collect requires --vault")
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    out_dir = args.out_dir or Path.cwd()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"tessera-bundle-{args.collect}-{stamp}"
    config = resolve_config(vault_path=args.vault)
    event_log: EventLog | None = None
    try:
        event_log = EventLog.open(config.events_db_path)
    except Exception as exc:
        # Missing or unreadable events.db is not a showstopper — the
        # bundle just reports an empty recent_events file. Surface a
        # one-liner so the operator understands the reduced content.
        print(f"(events.db unavailable: {type(exc).__name__}; recent_events will be empty)")
    try:
        with open_vault(args.vault, passphrase) as vc:
            active_models = tuple(
                m.name for m in models_registry.list_models(vc.connection) if m.is_active
            )
            spec = BundleSpec(
                vault_conn=vc.connection,
                vault_path=args.vault,
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
    print(review_instructions(result))
    return 0
