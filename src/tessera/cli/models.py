"""``tessera models [list|set|test]`` — fastembed model registry CLI.

Three subcommands:

- ``list``   — print the registered adapter slots (one entry, ``fastembed``).
- ``set``    — record a model identifier in the vault's ``embedding_models``
  table and optionally flag it active. ``--name`` is the fastembed model
  identifier (e.g. ``"nomic-ai/nomic-embed-text-v1.5"``); ``--dim`` must
  match the model's declared embedding dimensionality. The retrieval
  pipeline reads ``embedding_models.name`` directly to construct a
  :class:`FastEmbedEmbedder` per request.
- ``test``   — instantiate a :class:`FastEmbedEmbedder` for the given model
  identifier and run its ``health_check`` to confirm the weights load and
  the ONNX session embeds cleanly. Useful before the first daemon start
  to surface download / model-name failures interactively.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import sqlcipher3

from tessera.adapters import models_registry
from tessera.adapters.fastembed_embedder import DEFAULT_DIM, DEFAULT_MODEL, FastEmbedEmbedder
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
    set_parser.add_argument(
        "--name",
        required=True,
        help="fastembed model identifier, e.g. 'nomic-ai/nomic-embed-text-v1.5'",
    )
    set_parser.add_argument(
        "--dim",
        type=int,
        required=True,
        help="embedding dimensionality declared by the model (e.g. 768)",
    )
    set_parser.add_argument("--activate", action="store_true")

    test_parser = sub.add_parser("test", help="Load the fastembed model and run a health check.")
    test_parser.add_argument("--name", default=DEFAULT_MODEL)
    test_parser.add_argument("--dim", type=int, default=DEFAULT_DIM)

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
    embedder = FastEmbedEmbedder(model_name=args.name, dim=args.dim)
    with status(f"loading {args.name!r} via fastembed", emoji=EMOJI["models"]):
        try:
            asyncio.run(embedder.health_check())
        except Exception as exc:  # CLI top-level boundary: classify and exit non-zero
            error(f"health_check failed: {type(exc).__name__}: {exc}")
            return 1
    success(
        f"fastembed loaded {args.name!r} (dim={args.dim})",
        emoji=EMOJI["models"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
