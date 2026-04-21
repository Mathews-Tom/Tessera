"""``tessera init`` — bootstrap a fresh vault + default agent."""

from __future__ import annotations

import argparse
from pathlib import Path

from ulid import ULID

from tessera.cli._common import CliError, fail, resolve_passphrase
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
    with derive_key(passphrase, salt) as key:
        state = bootstrap(args.vault, key)
        with VaultConnection.open(args.vault, key) as vc:
            cur = vc.connection.execute(
                "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, 0)",
                (str(ULID()), args.agent_name),
            )
            agent_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    print(f"initialised vault at {args.vault}")
    print(f"  vault_id: {state.vault_id}")
    print(f"  schema: v{state.schema_version}")
    print(f"  agent: {args.agent_name} (id={agent_id})")
    print(f"  salt sidecar: {args.vault}.salt")
    print("Register an embedding model next:")
    print(f"  tessera models set --vault {args.vault} --passphrase ... --name ollama ...")
    return 0
