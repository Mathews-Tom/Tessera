"""``tessera audit verify`` — walk the audit-chain end-to-end.

Per ADR 0021 §Verify CLI. Exit codes:

* ``0`` — full chain integrity. Reports total rows, genesis row id,
  and the genesis timestamp.
* ``1`` — first broken row. Reports the row id, the recomputed
  ``row_hash``, the stored ``row_hash``, and the row's ``op``.
* ``2`` — schema or migration error (missing columns, vault not
  initialised, vault is mid-migration).

The verifier reads the vault file directly rather than going
through the daemon. This matches the use case (backup audits,
post-restore checks, sync round-trip verification) — the daemon may
not even be running on the host that is verifying. The command
takes the vault passphrase the same way every other vault-touching
CLI does (``--passphrase`` or ``$TESSERA_PASSPHRASE``).

Help text quotes the ADR 0021 claim boundary verbatim so users see
exactly what the chain detects and what it does not before they
treat ``exit 0`` as a stronger guarantee than it is.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._ui import EMOJI, info, success, warn
from tessera.vault.audit_chain import AuditChainBrokenError, verify_chain

_HELP_DESCRIPTION = (
    "Walk the audit-log forward hash chain end-to-end and detect tampering "
    "within the ADR 0021 claim boundary.\n\n"
    "DETECTS:\n"
    "  * Accidental corruption (truncated write, flipped bit, partial restore).\n"
    "  * Deletion, modification, reordering, or insertion of any audit row.\n\n"
    "DOES NOT DETECT:\n"
    "  * Tampering by an attacker who can recompute hashes (the chain payload\n"
    "    is unkeyed; the canonicalizer is published).\n"
    "  * Pre-upgrade tampering (rows written before V0.5-P8 were chained at\n"
    "    upgrade in their stored order; pre-upgrade edits are not retroactively\n"
    "    detectable).\n"
    "  * Loss of the entire audit_log table (no external anchor at v0.5).\n\n"
    "Exit codes: 0 = chain intact; 1 = broken row; 2 = schema/vault error."
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "audit",
        help="audit-log integrity tools",
        description="Verify the V0.5-P8 audit-log forward hash chain.",
    )
    audit_subparsers = parser.add_subparsers(dest="audit_command")
    verify_parser = audit_subparsers.add_parser(
        "verify",
        help="walk the audit-log hash chain end-to-end",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify_parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    verify_parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; falls back to $TESSERA_PASSPHRASE / keyring",
    )
    verify_parser.set_defaults(handler=_cmd_verify)
    parser.set_defaults(handler=_print_audit_help_when_no_subcommand(parser))


def _print_audit_help_when_no_subcommand(
    parser: argparse.ArgumentParser,
) -> Callable[[argparse.Namespace], int]:
    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _handler


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
    except CliError as exc:
        warn(str(exc))
        return 2
    if not vault_path.exists():
        warn(f"vault not found at {vault_path}")
        return 2
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        warn(str(exc))
        return 2
    try:
        with open_vault(vault_path, passphrase) as vc:
            try:
                outcome = verify_chain(vc.connection)
            except AuditChainBrokenError as exc:
                fail(str(exc))
                doctor_emoji = EMOJI.get("doctor", "")
                info(
                    f"first broken row: id={exc.row_id} op={exc.op!r}",
                    emoji=doctor_emoji,
                )
                info(
                    f"  expected row_hash: {exc.expected_row_hash}",
                    emoji=doctor_emoji,
                )
                info(
                    f"  stored   row_hash: {exc.actual_row_hash}",
                    emoji=doctor_emoji,
                )
                return 1
    except FileNotFoundError as exc:
        warn(str(exc))
        return 2
    except Exception as exc:
        # Schema or vault errors: missing columns (pre-V0.5-P8 vault
        # that was never upgraded), mid-migration, sqlcipher key
        # mismatch surfacing as an OperationalError. Surface the
        # message verbatim and return the schema-error exit code.
        warn(f"audit verify failed: {exc}")
        return 2
    if outcome.total_rows == 0:
        success("audit chain empty (no rows; chain is trivially intact)")
        return 0
    head = outcome.head
    success(
        f"audit chain intact: {outcome.total_rows} row(s); "
        f"genesis id={outcome.genesis_row_id} at={outcome.genesis_at}; "
        f"head id={head.row_id if head is not None else 'n/a'}"
    )
    return 0


__all__ = ["register"]
