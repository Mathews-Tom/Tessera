"""``tessera tokens {list,create,revoke}``."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.cli._common import (
    CliError,
    fail,
    open_vault,
    resolve_agent_id,
    resolve_passphrase,
)
from tessera.cli._ui import EMOJI, console, kv_panel, report_table, success, warn


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("tokens", help="manage capability tokens")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def _add_vault_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", type=Path, required=True)
        p.add_argument("--passphrase", default=None)

    list_p = sub.add_parser("list", help="list capability tokens")
    _add_vault_args(list_p)
    list_p.add_argument("--agent-id", type=int, default=None)
    list_p.set_defaults(handler=_cmd_list)

    create_p = sub.add_parser(
        "create", help="issue a new token (access + refresh for session/service)"
    )
    _add_vault_args(create_p)
    # --agent-id is optional. When omitted, the handler auto-selects the
    # single agent in the vault (the common case after `tessera init`,
    # which creates exactly one default agent). When the vault has zero
    # or >1 agents, the handler fails loud.
    create_p.add_argument(
        "--agent-id",
        type=int,
        default=None,
        help="agent id; defaults to the sole agent when the vault has exactly one",
    )
    create_p.add_argument("--client-name", required=True)
    create_p.add_argument(
        "--token-class",
        choices=["session", "service", "subagent"],
        default="session",
    )
    # Two parallel syntaxes for scope specification:
    #   --read X --read Y           (repeatable; backwards-compatible)
    #   --read-scope X,Y            (comma-separated; what the demo script uses)
    # Both forms feed into the same scope object; the handler concatenates.
    create_p.add_argument(
        "--read",
        action="append",
        default=[],
        help="facet_type grantable for read; repeat flag or pass * for all",
    )
    create_p.add_argument(
        "--write",
        action="append",
        default=[],
        help="facet_type grantable for write; repeat flag or pass * for all",
    )
    create_p.add_argument(
        "--read-scope",
        default=None,
        help="comma-separated facet_types for read scope (alternative to repeated --read)",
    )
    create_p.add_argument(
        "--write-scope",
        default=None,
        help="comma-separated facet_types for write scope (alternative to repeated --write)",
    )
    create_p.set_defaults(handler=_cmd_create)

    revoke_p = sub.add_parser("revoke", help="revoke a token by id")
    _add_vault_args(revoke_p)
    revoke_p.add_argument("--token-id", type=int, required=True)
    revoke_p.add_argument("--reason", default="operator_request")
    revoke_p.set_defaults(handler=_cmd_revoke)


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    with open_vault(args.vault, passphrase) as vc:
        sql = (
            "SELECT id, agent_id, client_name, token_class, expires_at, "
            "revoked_at FROM capabilities"
        )
        params: tuple[int, ...] = ()
        if args.agent_id is not None:
            sql += " WHERE agent_id = ?"
            params = (args.agent_id,)
        sql += " ORDER BY id"
        rows = vc.connection.execute(sql, params).fetchall()
    if not rows:
        console.print("[tessera.dim](no tokens)[/]")
        return 0
    table = report_table(
        "capability tokens",
        ["id", "agent", "client", "class", "expires_at", "revoked"],
        emoji=EMOJI["token"],
    )
    for r in rows:
        table.add_row(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            str(r[5]) if r[5] is not None else "",
        )
    console.print(table)
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    now_epoch = int(datetime.now(UTC).timestamp())
    read_list = _merge_scope_args(args.read, args.read_scope)
    write_list = _merge_scope_args(args.write, args.write_scope)
    scope = build_scope(read=read_list, write=write_list)
    with open_vault(args.vault, passphrase) as vc:
        try:
            agent_id = resolve_agent_id(vc.connection, args.agent_id)
        except CliError as exc:
            return fail(str(exc))
        issued = tokens.issue(
            vc.connection,
            agent_id=agent_id,
            client_name=args.client_name,
            token_class=args.token_class,
            scope=scope,
            now_epoch=now_epoch,
        )
    panel_items = {
        "token_id": str(issued.token_id),
        "access_token": issued.raw_token,
        "expires_at": str(issued.expires_at),
    }
    if issued.raw_refresh_token is not None:
        panel_items["refresh_token"] = issued.raw_refresh_token
    kv_panel("token issued", panel_items, emoji=EMOJI["token"])
    warn(
        "store these values now — the raw tokens are not recoverable from the vault",
    )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    now_epoch = int(datetime.now(UTC).timestamp())
    with open_vault(args.vault, passphrase) as vc:
        changed = tokens.revoke(
            vc.connection,
            token_id=args.token_id,
            now_epoch=now_epoch,
            reason=args.reason,
        )
    if not changed:
        return fail(f"token {args.token_id} is already revoked or does not exist")
    success(f"revoked token {args.token_id}", emoji=EMOJI["forget"])
    return 0


def _merge_scope_args(repeated: list[str], comma_separated: str | None) -> list[str]:
    """Merge the two scope-specification forms into one list.

    The CLI accepts ``--read X --read Y`` (repeatable) and
    ``--read-scope X,Y`` (comma-separated) as equivalents; this helper
    accepts both and concatenates. Leading/trailing whitespace inside
    the comma-separated form is trimmed. Empty entries are dropped so
    ``--read-scope ""`` does not silently grant empty scope.
    """

    merged: list[str] = list(repeated or [])
    if comma_separated:
        merged.extend(item.strip() for item in comma_separated.split(",") if item.strip())
    return merged


# ``resolve_agent_id`` lives in tessera.cli._common now so `tessera
# connect` shares the same "one agent = auto-select, many =
# disambiguate" contract.
