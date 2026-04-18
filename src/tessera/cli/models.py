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
import sys
from pathlib import Path

import sqlcipher3

from tessera.adapters import models_registry
from tessera.adapters.ollama_embedder import OllamaEmbedder
from tessera.adapters.registry import list_embedders, list_rerankers
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import derive_key, new_salt


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
    set_parser.add_argument("--vault", type=Path, required=True)
    set_parser.add_argument("--passphrase", required=True)
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
    print("python adapters:")
    print(f"  embedders: {list_embedders()}")
    print(f"  rerankers: {list_rerankers()}")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    passphrase = args.passphrase.encode("utf-8")
    salt = new_salt()
    with derive_key(passphrase, salt) as key, VaultConnection.open(args.vault, key) as vc:
        conn: sqlcipher3.Connection = vc.connection
        model = models_registry.register_embedding_model(
            conn, name=args.name, dim=args.dim, activate=args.activate
        )
    print(f"registered: id={model.id} name={model.name} dim={model.dim} active={model.is_active}")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    embedder = OllamaEmbedder(model_name=args.model, dim=args.dim, host=args.host)
    try:
        asyncio.run(embedder.health_check())
    except Exception as exc:  # CLI top-level boundary: classify and exit non-zero
        print(f"health_check failed: {exc}", file=sys.stderr)
        return 1
    print(f"ollama reachable; model {args.model!r} is present")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
