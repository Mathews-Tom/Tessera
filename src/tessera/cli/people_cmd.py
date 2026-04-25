"""``tessera people [list|show|merge|split]``.

Same transport split as ``tessera skills``: ``list`` and ``show`` go
through HTTP MCP (``list_people``, ``resolve_person``); ``merge`` and
``split`` open the vault directly because they mutate the people +
person_mentions graph and have no MCP equivalent at v0.3.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tessera.cli._common import CliError, fail, open_vault, resolve_passphrase
from tessera.cli._http import add_http_args, call, print_json
from tessera.cli._ui import EMOJI, console, report_table, status, success
from tessera.vault import people as vault_people


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    people = subparsers.add_parser("people", help="manage people referenced in your facets")
    sub = people.add_subparsers(dest="people_command", required=True)

    list_p = sub.add_parser("list", help="list people via the running daemon")
    add_http_args(list_p)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument(
        "--since", type=int, default=None, help="filter by created_at epoch (seconds)"
    )
    list_p.set_defaults(handler=_cmd_list)

    show_p = sub.add_parser(
        "show",
        help="resolve a free-form mention to candidate people via the running daemon",
    )
    add_http_args(show_p)
    show_p.add_argument("mention")
    show_p.set_defaults(handler=_cmd_show)

    merge_p = sub.add_parser(
        "merge",
        help="collapse two people rows into one (direct vault access)",
    )
    merge_p.add_argument("--primary", required=True, help="surviving external_id")
    merge_p.add_argument("--secondary", required=True, help="external_id to merge into primary")
    _add_vault_args(merge_p)
    merge_p.set_defaults(handler=_cmd_merge)

    split_p = sub.add_parser(
        "split",
        help="extract a new person row out of an existing one (direct vault access)",
    )
    split_p.add_argument("--person", required=True, help="external_id of the row to split")
    split_p.add_argument("--canonical", required=True, help="canonical name for the extracted row")
    split_p.add_argument(
        "--aliases",
        default="",
        help="comma-separated aliases to move from the original to the new row",
    )
    _add_vault_args(split_p)
    split_p.set_defaults(handler=_cmd_split)


def _add_vault_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; default is $TESSERA_PASSPHRASE",
    )
    parser.add_argument("--agent-id", type=int, default=None)


def _cmd_list(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"limit": args.limit}
    if args.since is not None:
        payload["since"] = args.since
    with status("listing people", emoji=EMOJI["recall"]):
        try:
            result = call(args, "list_people", payload)
        except SystemExit as exc:
            return fail(str(exc))
    items = result.get("items")
    if isinstance(items, list) and console.is_terminal:
        table = report_table(
            "people",
            ["canonical_name", "aliases", "external_id"],
            emoji=EMOJI["recall"],
        )
        for p in items:
            if not isinstance(p, dict):
                continue
            aliases = p.get("aliases")
            alias_str = ", ".join(aliases) if isinstance(aliases, list) else ""
            table.add_row(
                str(p.get("canonical_name", "")),
                alias_str,
                str(p.get("external_id", "")),
            )
        console.print(table)
    else:
        print_json(result)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with status(f"resolve person {args.mention!r}", emoji=EMOJI["recall"]):
        try:
            result = call(args, "resolve_person", {"mention": args.mention})
        except SystemExit as exc:
            return fail(str(exc))
    print_json(result)
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    # merge is keyed on the two external_ids; agent_id is recovered
    # internally from the rows. ``--agent-id`` is kept on the parser
    # for symmetry with split (which does need it for the new-row
    # insert) but unused here.
    try:
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(args.vault, passphrase) as vc,
            status(f"merge {args.secondary} → {args.primary}", emoji=EMOJI["repair"]),
        ):
            survivor = vault_people.merge(
                vc.connection,
                primary_external_id=args.primary,
                secondary_external_id=args.secondary,
            )
    except CliError as exc:
        return fail(str(exc))
    except vault_people.PeopleError as exc:
        return fail(str(exc))
    success(
        f"merged {args.secondary} into {args.primary} ({len(survivor.aliases)} aliases retained)",
        emoji=EMOJI["repair"],
    )
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    move_aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]
    try:
        passphrase = resolve_passphrase(args.passphrase)
        with (
            open_vault(args.vault, passphrase) as vc,
            status(f"split {args.person} → {args.canonical!r}", emoji=EMOJI["repair"]),
        ):
            _, new_person = vault_people.split(
                vc.connection,
                person_external_id=args.person,
                extracted_canonical_name=args.canonical,
                move_aliases=move_aliases or None,
            )
    except CliError as exc:
        return fail(str(exc))
    except vault_people.PeopleError as exc:
        return fail(str(exc))
    success(
        f"created {new_person.canonical_name!r} ({new_person.external_id})",
        emoji=EMOJI["repair"],
    )
    return 0
