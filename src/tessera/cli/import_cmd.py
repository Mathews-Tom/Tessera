"""``tessera import {chatgpt} <path>`` — convo-history importers.

The importers open the vault directly (filesystem-walk + bulk insert
do not have an MCP equivalent) and print a summary table on the way
out. ``tessera import claude`` lands in phase 7; the parser stub for
it is registered here under a placeholder so the v0.3 surface keeps a
stable command line.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tessera.cli._common import CliError, fail, open_vault, resolve_agent_id, resolve_passphrase
from tessera.cli._http import print_json
from tessera.cli._ui import EMOJI, console, error, report_table, status, success
from tessera.importers import chatgpt as chatgpt_importer

_IMPORTABLE_FACET_TYPES: tuple[str, ...] = (
    "identity",
    "preference",
    "workflow",
    "project",
    "style",
)


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
    chatgpt.add_argument(
        "--facet-type",
        choices=_IMPORTABLE_FACET_TYPES,
        default="project",
        help="v0.1 facet type to write each conversation as (default: project)",
    )
    chatgpt.add_argument(
        "--source-tool",
        default="chatgpt-import",
        help="source_tool tag to attach to imported facets",
    )
    _add_vault_args(chatgpt)
    chatgpt.set_defaults(handler=_cmd_chatgpt)


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; default is $TESSERA_PASSPHRASE",
    )
    parser.add_argument("--agent-id", type=int, default=None)


def _cmd_chatgpt(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(args.vault, passphrase) as vc,
            status(f"importing {args.export_path}", emoji=EMOJI["repair"]),
        ):
            agent_id = resolve_agent_id(vc.connection, args.agent_id)
            report = chatgpt_importer.import_export(
                vc.connection,
                agent_id=agent_id,
                export_path=args.export_path,
                source_tool=args.source_tool,
                facet_type=args.facet_type,
            )
    except CliError as exc:
        return fail(str(exc))
    except chatgpt_importer.ImportError_ as exc:
        return fail(str(exc))
    _render_report(report)
    return 1 if report.errors else 0


def _render_report(report: chatgpt_importer.ImportReport) -> None:
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
