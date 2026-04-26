"""Stub CLI surface for ``tessera models [list|set|test]``.

The full CLI is P9; this module is the narrow slice needed to satisfy the P2
exit gate ("``tessera models list/set/test`` stub works against Ollama
adapter"). It is deliberately small — argparse rather than Typer, no daemon
control — so the P9 rewrite does not have to preserve any contract from here.

Invocation: ``python -m tessera.cli.models <subcommand> [args]``.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import sqlcipher3

from tessera.adapters import models_registry
from tessera.adapters.ollama_embedder import OllamaEmbedder
from tessera.adapters.registry import list_embedders, list_rerankers
from tessera.cli._common import CliError, resolve_passphrase, resolve_vault_path
from tessera.cli._ui import EMOJI, console, error, report_table, status, success
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, load_salt


def run(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "set":
        return _cmd_set(args)
    if args.command == "test":
        return _cmd_test(args)
    parser.print_help()
    return 2


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tessera models")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List registered adapters and vault-registered models.")

    set_parser = sub.add_parser("set", help="Register an embedding model in a vault.")
    set_parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="vault path; default $TESSERA_VAULT or ~/.tessera/vault.db",
    )
    set_parser.add_argument(
        "--passphrase",
        default=None,
        help="vault passphrase; default is $TESSERA_PASSPHRASE",
    )
    set_parser.add_argument("--name", required=True, help="Adapter name, e.g. 'ollama'")
    set_parser.add_argument("--model", required=True, help="Provider model name")
    set_parser.add_argument("--dim", type=int, required=True)
    set_parser.add_argument("--activate", action="store_true")

    test_parser = sub.add_parser("test", help="Health-check the ollama embedder.")
    test_parser.add_argument("--model", default="nomic-embed-text")
    test_parser.add_argument("--dim", type=int, default=768)
    test_parser.add_argument("--host", default="http://localhost:11434")

    return parser


def _cmd_list() -> int:
    table = report_table(
        "python adapters",
        ["role", "registered"],
        emoji=EMOJI["models"],
    )
    table.add_row("embedders", ", ".join(list_embedders()) or "(none)")
    table.add_row("rerankers", ", ".join(list_rerankers()) or "(none)")
    console.print(table)
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    try:
        vault_path = resolve_vault_path(args.vault)
        passphrase = resolve_passphrase(args.passphrase)
    except CliError as exc:
        error(str(exc))
        return 1
    try:
        salt = load_salt(vault_path)
    except FileNotFoundError:
        error(f"no KDF salt sidecar for {vault_path}; initialise the vault first")
        return 1
    with (
        status(f"registering {args.name} ({args.dim}-dim)", emoji=EMOJI["models"]),
        derive_key(passphrase, salt) as key,
        VaultConnection.open(vault_path, key) as vc,
    ):
        conn: sqlcipher3.Connection = vc.connection
        model = models_registry.register_embedding_model(
            conn, name=args.name, dim=args.dim, activate=args.activate
        )
    success(
        f"registered id={model.id} name={model.name} dim={model.dim} active={model.is_active}",
        emoji=EMOJI["models"],
    )
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    embedder = OllamaEmbedder(model_name=args.model, dim=args.dim, host=args.host)
    with status(f"probing ollama for {args.model!r}", emoji=EMOJI["models"]):
        try:
            asyncio.run(embedder.health_check())
        except Exception as exc:  # CLI top-level boundary: classify and exit non-zero
            error(f"health_check failed: {exc}")
            return 1
    success(f"ollama reachable; model {args.model!r} is present", emoji=EMOJI["models"])
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
