"""``tessera init`` — bootstrap a fresh vault + default agent."""

from __future__ import annotations

import argparse
from pathlib import Path

from ulid import ULID

from tessera.cli._common import CliError, fail, resolve_passphrase, resolve_vault_path
from tessera.cli._ui import EMOJI, info, kv_panel, status, success
from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt, save_salt


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("init", help="bootstrap a fresh vault")
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    parser.add_argument("--passphrase", default=None)
    parser.add_argument("--agent-name", default="default")
    parser.set_defaults(handler=_cmd_init)


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        return fail(str(exc))
    if vault_path.exists():
        return fail(f"{vault_path} already exists; refusing to overwrite")
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    salt = new_salt()
    save_salt(vault_path, salt)
    with (
        status(f"bootstrapping vault at {vault_path}", emoji=EMOJI["vault"]),
        derive_key(passphrase, salt) as key,
    ):
        state = bootstrap(vault_path, key)
        with VaultConnection.open(vault_path, key) as vc:
            cur = vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 0)",
                (str(ULID()), args.agent_name),
            )
            agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    success(f"initialised vault at {vault_path}", emoji=EMOJI["vault"])
    kv_panel(
        "vault",
        {
            "vault_id": state.vault_id,
            "schema": f"v{state.schema_version}",
            "agent": f"{args.agent_name} (id={agent_id})",
            "salt sidecar": f"{vault_path}.salt",
        },
        emoji=EMOJI["vault"],
    )
    info(
        "next: tessera models set --name nomic-ai/nomic-embed-text-v1.5 --dim 768 --activate",
        emoji=EMOJI["models"],
    )
    return 0
