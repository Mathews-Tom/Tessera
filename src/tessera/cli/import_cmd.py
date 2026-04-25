"""``tessera import {chatgpt|claude} <path>`` — convo-history importers.

The importers open the vault directly (filesystem-walk + bulk insert
do not have an MCP equivalent) and print a summary table on the way
out. Each subcommand is a thin shell around the matching parser in
``tessera.importers``; the report dataclass and error hierarchy are
shared via ``importers._common`` so both subcommands go through one
render path.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

import sqlcipher3

from tessera.cli._common import CliError, fail, open_vault, resolve_agent_id, resolve_passphrase
from tessera.cli._http import print_json
from tessera.cli._ui import EMOJI, console, error, report_table, status, success
from tessera.importers import chatgpt as chatgpt_importer
from tessera.importers import claude as claude_importer
from tessera.importers._common import (
    IMPORTABLE_FACET_TYPES,
    ImportError_,
    ImportReport,
)

# Each vendor importer takes the same keyword set and returns the
# shared ImportReport. Typing the dispatch helper against this alias
# rather than a per-vendor module keeps the boundary free to gain
# additional importers without touching the dispatcher signature.
_ImportFn = Callable[..., ImportReport]

_IMPORTABLE_FACET_TYPE_CHOICES: tuple[str, ...] = tuple(sorted(IMPORTABLE_FACET_TYPES))


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    importer = subparsers.add_parser(
        "import",
        help="import conversation history from an external tool",
    )
    sub = importer.add_subparsers(dest="import_command", required=True)

    chatgpt = sub.add_parser(
        "chatgpt",
        help="import a ChatGPT conversations.json export",
    )
    chatgpt.add_argument(
        "export_path",
        type=Path,
        help="path to conversations.json from a ChatGPT data export",
    )
    _add_common_import_args(chatgpt, default_source_tool="chatgpt-import")
    chatgpt.set_defaults(handler=_cmd_chatgpt)

    claude = sub.add_parser(
        "claude",
        help="import a Claude conversations.json data export",
    )
    claude.add_argument(
        "export_path",
        type=Path,
        help="path to conversations.json from a Claude data export",
    )
    _add_common_import_args(claude, default_source_tool="claude-import")
    claude.set_defaults(handler=_cmd_claude)


def _add_common_import_args(parser: argparse.ArgumentParser, *, default_source_tool: str) -> None:
    parser.add_argument(
        "--facet-type",
        choices=_IMPORTABLE_FACET_TYPE_CHOICES,
        default="project",
        help="v0.1 facet type to write each conversation as (default: project)",
    )
    parser.add_argument(
        "--source-tool",
        default=default_source_tool,
        help="source_tool tag to attach to imported facets",
    )
    _add_vault_args(parser)


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; default is $TESSERA_PASSPHRASE",
    )
    parser.add_argument("--agent-id", type=int, default=None)


def _cmd_chatgpt(args: argparse.Namespace) -> int:
    return _run_import(args, "chatgpt", chatgpt_importer.import_export)


def _cmd_claude(args: argparse.Namespace) -> int:
    return _run_import(args, "claude", claude_importer.import_export)


def _run_import(
    args: argparse.Namespace,
    vendor: str,
    import_export: _ImportFn,
) -> int:
    """Open the vault and dispatch to the vendor's ``import_export``.

    Both vendor parsers expose an ``import_export(conn, *, agent_id,
    export_path, source_tool, facet_type) -> ImportReport`` signature;
    the ``_ImportFn`` alias captures that contract so adding a third
    vendor costs only a new module + one ``set_defaults`` call.
    """

    try:
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(args.vault, passphrase) as vc,
            status(f"importing {args.export_path} ({vendor})", emoji=EMOJI["repair"]),
        ):
            agent_id = resolve_agent_id(vc.connection, args.agent_id)
            report = _invoke_importer(import_export, vc.connection, args, agent_id)
    except CliError as exc:
        return fail(str(exc))
    except ImportError_ as exc:
        return fail(str(exc))
    _render_report(report)
    return 1 if report.errors else 0


def _invoke_importer(
    import_export: _ImportFn,
    conn: sqlcipher3.Connection,
    args: argparse.Namespace,
    agent_id: int,
) -> ImportReport:
    return import_export(
        conn,
        agent_id=agent_id,
        export_path=args.export_path,
        source_tool=args.source_tool,
        facet_type=args.facet_type,
    )


def _render_report(report: ImportReport) -> None:
    if console.is_terminal:
        table = report_table(
            "import",
            ["seen", "created", "deduplicated", "skipped_empty", "errors"],
            emoji=EMOJI["repair"],
        )
        table.add_row(
            str(report.conversations_seen),
            str(report.facets_created),
            str(report.facets_deduplicated),
            str(report.skipped_empty),
            str(len(report.errors)),
        )
        console.print(table)
    else:
        print_json(
            {
                "conversations_seen": report.conversations_seen,
                "facets_created": report.facets_created,
                "facets_deduplicated": report.facets_deduplicated,
                "skipped_empty": report.skipped_empty,
                "errors": list(report.errors),
                "source_path": report.source_path,
            }
        )
    for line in report.errors:
        error(line)
    if not report.errors:
        success(
            f"imported {report.facets_created} facet(s); "
            f"{report.facets_deduplicated} dedup, {report.skipped_empty} empty",
            emoji=EMOJI["repair"],
        )
