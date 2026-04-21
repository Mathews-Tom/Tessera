"""``tessera doctor`` — health-check matrix."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from tessera.cli._common import CliError, fail, open_vault, resolve_passphrase
from tessera.daemon.config import resolve_config
from tessera.daemon.doctor import DoctorStatus, run_all


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("doctor", help="run health checks")
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--passphrase", default=None)
    parser.set_defaults(handler=_cmd_doctor)


def _cmd_doctor(args: argparse.Namespace) -> int:
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
