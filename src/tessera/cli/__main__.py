"""``python -m tessera.cli`` — top-level CLI entrypoint.

Dispatches subcommands to focused handlers. Commands that need a
running daemon talk to it over the Unix control socket
(``tessera daemon {status,stop}``) or the HTTP MCP endpoint
(``tessera {capture,recall,show,stats}``). Commands that operate on a
locked vault unlock it directly (``tessera init``,
``tessera tokens``, ``tessera agents``, ``tessera doctor``).

This module stays argparse-based to match ``tessera.cli.models`` and
``tessera.cli.vault``; switching one subtree to Typer would create a
split-brain user experience.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from tessera import __version__ as TESSERA_VERSION
from tessera.cli import (
    agents_cmd,
    connect_cmd,
    curl_cmd,
    daemon_cmd,
    doctor_cmd,
    export_cmd,
    import_cmd,
    init_cmd,
    people_cmd,
    skills_cmd,
    tokens_cmd,
    tools_cmd,
)
from tessera.cli import models as models_cli
from tessera.cli import vault as vault_cli

_DelegateRun = Callable[[list[str] | None], int]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        from tessera.cli._ui import warn

        warn("interrupted")
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tessera")
    parser.add_argument(
        "--version",
        action="version",
        version=f"tessera {TESSERA_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_cmd.register(subparsers)
    daemon_cmd.register(subparsers)
    agents_cmd.register(subparsers)
    tokens_cmd.register(subparsers)
    tools_cmd.register(subparsers)
    doctor_cmd.register(subparsers)
    connect_cmd.register(subparsers)
    export_cmd.register(subparsers)
    skills_cmd.register(subparsers)
    people_cmd.register(subparsers)
    import_cmd.register(subparsers)
    curl_cmd.register(subparsers)

    # Existing stubs from earlier phases. Each of these delegates its
    # argv slice to a submodule's own argparse parser; argparse.REMAINDER
    # lets the outer parser accept arbitrary trailing arguments instead
    # of rejecting them. Without this, `tessera models set ...` would
    # fail at the outer parser before reaching the delegate.
    models_parser = subparsers.add_parser(
        "models", help="embedding / reranker adapters", add_help=False
    )
    models_parser.add_argument("rest", nargs=argparse.REMAINDER)
    models_parser.set_defaults(handler=_delegate(models_cli.run))
    vault_parser = subparsers.add_parser("vault", help="vault maintenance", add_help=False)
    vault_parser.add_argument("rest", nargs=argparse.REMAINDER)
    vault_parser.set_defaults(handler=_delegate(vault_cli.run))

    stdio_parser = subparsers.add_parser(
        "stdio",
        help="stdio ↔ HTTP MCP bridge (used by Claude Desktop; speaks stdio to the parent, forwards over HTTP to the Tessera daemon)",
    )
    stdio_parser.add_argument(
        "--url",
        required=True,
        help="Tessera daemon HTTP MCP endpoint, e.g. http://127.0.0.1:5710/mcp",
    )
    stdio_parser.add_argument(
        "--token",
        required=True,
        help="bearer token minted by `tessera tokens create` or `tessera connect`",
    )
    stdio_parser.set_defaults(handler=_run_stdio_bridge)

    return parser


def _delegate(run_fn: _DelegateRun) -> Callable[[argparse.Namespace], int]:
    """Wrap a submodule ``run(argv)`` so it matches the handler signature."""

    def _handler(args: argparse.Namespace) -> int:
        del args
        # Recover the original argv slice after the top-level subcommand.
        idx = sys.argv.index(sys.argv[1]) if len(sys.argv) > 1 else 1
        return int(run_fn(sys.argv[idx + 1 :]))

    return _handler


def _run_stdio_bridge(args: argparse.Namespace) -> int:
    from tessera.daemon.stdio_bridge import run

    return run(args.url, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
