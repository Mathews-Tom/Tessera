"""``tessera skills [list|show|sync-to-disk|sync-from-disk]``.

The four subcommands split into two transports. ``list`` and ``show``
talk to the running daemon over HTTP MCP (``list_skills`` /
``get_skill``) so a Claude Desktop user with only a token and no
passphrase can still inspect the skills surface. ``sync-to-disk``
and ``sync-from-disk`` open the vault directly because the operation
is filesystem I/O the daemon does not expose; the user is expected
to stop the daemon (or accept the WAL race) before running a sync.

The disk-sync commands print a compact summary table and exit
nonzero when any per-file error was collected — the report itself
contains the per-file error strings so the failure surface is
inspectable rather than hidden behind a single exit code.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_agent_id,
    resolve_passphrase,
    resolve_vault_path,
)
from tessera.cli._http import add_http_args, call, print_json
from tessera.cli._ui import EMOJI, console, error, report_table, status, success
from tessera.vault import skills as vault_skills


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    skills = subparsers.add_parser("skills", help="manage learned skills (procedure markdown)")
    sub = skills.add_subparsers(dest="skills_command", required=True)

    list_p = sub.add_parser("list", help="list skills via the running daemon")
    add_http_args(list_p)
    list_p.add_argument(
        "--all",
        action="store_true",
        help="include retired (active=False) skills (default: active only)",
    )
    list_p.add_argument("--limit", type=int, default=50)
    list_p.set_defaults(handler=_cmd_list)

    show_p = sub.add_parser("show", help="show one skill by exact name via the running daemon")
    add_http_args(show_p)
    show_p.add_argument("name")
    show_p.set_defaults(handler=_cmd_show)

    sync_to = sub.add_parser(
        "sync-to-disk",
        help="write every active skill as {slug}.md under DIR (direct vault access)",
    )
    sync_to.add_argument("directory", type=Path)
    _add_vault_args(sync_to)
    sync_to.set_defaults(handler=_cmd_sync_to_disk)

    sync_from = sub.add_parser(
        "sync-from-disk",
        help="reconcile DIR's .md files into skills (direct vault access)",
    )
    sync_from.add_argument("directory", type=Path)
    sync_from.add_argument("--source-tool", default="cli", help="source_tool tag for new rows")
    _add_vault_args(sync_from)
    sync_from.set_defaults(handler=_cmd_sync_from_disk)


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; default is $TESSERA_PASSPHRASE",
    )
    parser.add_argument("--agent-id", type=int, default=None)


def _cmd_list(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"limit": args.limit, "active_only": not args.all}
    with status("listing skills", emoji=EMOJI["recall"]):
        try:
            result = call(args, "list_skills", payload)
        except SystemExit as exc:
            return fail(str(exc))
    items = result.get("items")
    if isinstance(items, list) and console.is_terminal:
        table = report_table(
            "skills",
            ["name", "active", "external_id", "description"],
            emoji=EMOJI["recall"],
        )
        for s in items:
            if not isinstance(s, dict):
                continue
            table.add_row(
                str(s.get("name", "")),
                "yes" if s.get("active") else "no",
                str(s.get("external_id", "")),
                str(s.get("description", ""))[:60],
            )
        console.print(table)
    else:
        print_json(result)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with status(f"show skill {args.name!r}", emoji=EMOJI["recall"]):
        try:
            result = call(args, "get_skill", {"name": args.name})
        except SystemExit as exc:
            return fail(str(exc))
    print_json(result)
    return 0


def _cmd_sync_to_disk(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(vault_path, passphrase) as vc,
            status(f"sync-to-disk → {args.directory}", emoji=EMOJI["repair"]),
        ):
            agent_id = resolve_agent_id(vc.connection, args.agent_id)
            report = vault_skills.sync_to_disk(
                vc.connection, agent_id=agent_id, base_dir=args.directory
            )
    except CliError as exc:
        return fail(str(exc))
    _render_to_disk_report(report)
    return 1 if report.errors else 0


def _cmd_sync_from_disk(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(vault_path, passphrase) as vc,
            status(f"sync-from-disk ← {args.directory}", emoji=EMOJI["repair"]),
        ):
            agent_id = resolve_agent_id(vc.connection, args.agent_id)
            report = vault_skills.sync_from_disk(
                vc.connection,
                agent_id=agent_id,
                base_dir=args.directory,
                source_tool=args.source_tool,
            )
    except CliError as exc:
        return fail(str(exc))
    _render_from_disk_report(report)
    return 1 if report.errors else 0


def _render_to_disk_report(report: vault_skills.SyncToDiskReport) -> None:
    if console.is_terminal:
        table = report_table(
            "sync-to-disk", ["written", "skipped", "errors"], emoji=EMOJI["repair"]
        )
        table.add_row(str(report.written), str(report.skipped), str(len(report.errors)))
        console.print(table)
    else:
        print_json(
            {
                "written": report.written,
                "skipped": report.skipped,
                "errors": list(report.errors),
                "paths": list(report.paths),
            }
        )
    for line in report.errors:
        error(line)
    if not report.errors:
        success(
            f"wrote {report.written} skill(s); skipped {report.skipped}",
            emoji=EMOJI["repair"],
        )


def _render_from_disk_report(report: vault_skills.SyncFromDiskReport) -> None:
    if console.is_terminal:
        table = report_table(
            "sync-from-disk",
            ["imported", "updated", "unchanged", "errors"],
            emoji=EMOJI["repair"],
        )
        table.add_row(
            str(report.imported),
            str(report.updated),
            str(report.unchanged),
            str(len(report.errors)),
        )
        console.print(table)
    else:
        print_json(
            {
                "imported": report.imported,
                "updated": report.updated,
                "unchanged": report.unchanged,
                "errors": list(report.errors),
                "paths": list(report.paths),
            }
        )
    for line in report.errors:
        error(line)
    if not report.errors:
        success(
            f"imported {report.imported}, updated {report.updated}, unchanged {report.unchanged}",
            emoji=EMOJI["repair"],
        )
