"""``tessera init`` — bootstrap a fresh vault + default agent."""

from __future__ import annotations

import argparse
from pathlib import Path

from ulid import ULID

from tessera.cli._common import CliError, fail, resolve_passphrase
from tessera.cli._ui import EMOJI, info, kv_panel, status, success
from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("init", help="bootstrap a fresh vault")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--passphrase", default=None)
    parser.add_argument("--agent-name", default="default")
    parser.set_defaults(handler=_cmd_init)


def _cmd_init(args: argparse.Namespace) -> int:
    if args.vault.exists():
        return fail(f"{args.vault} already exists; refusing to overwrite")
    try:
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    args.vault.parent.mkdir(parents=True, exist_ok=True)
    salt = new_salt()
    save_salt(args.vault, salt)
    with (
        status(f"bootstrapping vault at {args.vault}", emoji=EMOJI["vault"]),
        derive_key(passphrase, salt) as key,
    ):
        state = bootstrap(args.vault, key)
        with VaultConnection.open(args.vault, key) as vc:
            cur = vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 0)",
                (str(ULID()), args.agent_name),
            )
            agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    success(f"initialised vault at {args.vault}", emoji=EMOJI["vault"])
    kv_panel(
        "vault",
        {
            "vault_id": state.vault_id,
            "schema": f"v{state.schema_version}",
            "agent": f"{args.agent_name} (id={agent_id})",
            "salt sidecar": f"{args.vault}.salt",
        },
        emoji=EMOJI["vault"],
    )
    info(
        f"next: tessera models set --vault {args.vault} --passphrase ... --name ollama ...",
        emoji=EMOJI["models"],
    )
    return 0
