"""``tessera agents {list,create,delete}``."""

from __future__ import annotations

import argparse
from pathlib import Path

from ulid import ULID

from tessera.cli._common import CliError, fail, open_vault, resolve_passphrase
from tessera.cli._ui import EMOJI, console, raw, report_table, success


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("agents", help="manage agents")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def _add_vault_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", type=Path, required=True)
        p.add_argument("--passphrase", default=None)

    list_p = sub.add_parser("list", help="list agents")
    _add_vault_args(list_p)
    list_p.set_defaults(handler=_cmd_list)

    create_p = sub.add_parser("create", help="create an agent")
    _add_vault_args(create_p)
    create_p.add_argument("--name", required=True)
    create_p.set_defaults(handler=_cmd_create)

    delete_p = sub.add_parser("delete", help="delete an agent by external_id")
    _add_vault_args(delete_p)
    delete_p.add_argument("--external-id", required=True)
    delete_p.set_defaults(handler=_cmd_delete)


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    with open_vault(args.vault, passphrase) as vc:
        rows = vc.connection.execute(
            "SELECT external_id, name, created_at FROM agents ORDER BY id"
        ).fetchall()
    if not rows:
        console.print("[tessera.dim](no agents)[/]")
        return 0
    table = report_table("agents", ["external_id", "name", "created_at"], emoji=EMOJI["agent"])
    for row in rows:
        table.add_row(str(row[0]), str(row[1]), str(row[2]))
    console.print(table)
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    external_id = str(ULID())
    with open_vault(args.vault, passphrase) as vc:
        vc.connection.execute(
            "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, strftime('%s','now'))",
            (external_id, args.name),
        )
    # The ULID is the machine-readable output of this command. Scripts
    # pipe it to downstream consumers (e.g. ``id=$(tessera agents
    # create --vault X --name foo)``). Emit through raw() on stdout
    # with nothing else — the absence of a red ✗ is itself the success
    # signal on the TTY side.
    raw(external_id)
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    with open_vault(args.vault, passphrase) as vc:
        cur = vc.connection.execute("DELETE FROM agents WHERE external_id = ?", (args.external_id,))
        if cur.rowcount == 0:
            return fail(f"no agent with external_id={args.external_id!r}")
    success(f"deleted {args.external_id}", emoji=EMOJI["forget"])
    return 0
