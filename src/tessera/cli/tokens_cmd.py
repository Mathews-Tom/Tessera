"""``tessera tokens {list,create,revoke}``."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.cli._common import CliError, fail, open_vault, resolve_passphrase


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
    create_p.add_argument("--agent-id", type=int, required=True)
    create_p.add_argument("--client-name", required=True)
    create_p.add_argument(
        "--token-class",
        choices=["session", "service", "subagent"],
        default="session",
    )
    create_p.add_argument(
        "--read",
        action="append",
        default=[],
        help="facet_type grantable for read; repeat or pass * for all",
    )
    create_p.add_argument(
        "--write",
        action="append",
        default=[],
        help="facet_type grantable for write; repeat or pass * for all",
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
        print("(no tokens)")
        return 0
    print("id\tagent\tclient\tclass\texpires_at\trevoked")
    for r in rows:
        print(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\t{r[5] or ''}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    now_epoch = int(datetime.now(UTC).timestamp())
    scope = build_scope(read=args.read or [], write=args.write or [])
    with open_vault(args.vault, passphrase) as vc:
        issued = tokens.issue(
            vc.connection,
            agent_id=args.agent_id,
            client_name=args.client_name,
            token_class=args.token_class,
            scope=scope,
            now_epoch=now_epoch,
        )
    print(f"token_id: {issued.token_id}")
    print(f"access_token: {issued.raw_token}")
    if issued.raw_refresh_token is not None:
        print(f"refresh_token: {issued.raw_refresh_token}")
    print(f"expires_at: {issued.expires_at}")
    print(
        "Store these values now — the access and refresh tokens are not recoverable from the vault."
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
    print(f"revoked token {args.token_id}")
    return 0
