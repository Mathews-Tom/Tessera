"""Shared HTTP-MCP helpers for CLI subcommands.

Subcommands that call into the running daemon (``tessera capture``,
``tessera skills list``, ``tessera people show``, …) all need the
same four primitives: register the ``--host``/``--port``/``--token``
flags, resolve the bearer token from CLI args or the environment,
issue a single MCP method call, and pretty-print the JSON envelope.
This module owns those primitives so each subcommand stays a thin
shell over its tool name.

The HTTP shape is the Tessera-native envelope (``{"method": str,
"args": dict}`` → ``{"ok": bool, "result"?: dict, "error"?: ...}``);
canonical MCP Streamable HTTP is wrapped on the stdio side via
``daemon.stdio_bridge``.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

from tessera.cli._ui import console
from tessera.daemon.config import DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT


def add_http_args(parser: argparse.ArgumentParser) -> None:
    """Attach the standard ``--host``/``--port``/``--token`` flags."""

    parser.add_argument("--host", default=DEFAULT_HTTP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument(
        "--token",
        default=None,
        help="bearer token; default is $TESSERA_TOKEN",
    )


def resolve_token(args: argparse.Namespace) -> str:
    """Pick the bearer token from --token or $TESSERA_TOKEN.

    Raises :class:`SystemExit` when neither is set so the calling
    subcommand surfaces a single error path through ``fail()``.
    """

    token = args.token or os.environ.get("TESSERA_TOKEN")
    if not token:
        raise SystemExit("access token required; pass --token or export TESSERA_TOKEN")
    return token


def call(args: argparse.Namespace, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Issue one Tessera-envelope MCP call. Returns the unwrapped result.

    The bearer token comes from :func:`resolve_token`; transport
    failures and non-200 statuses raise :class:`SystemExit` so the
    subcommand's outer error handler renders one stable failure path
    regardless of HTTP / envelope / shape problems.
    """

    token = resolve_token(args)
    url = f"http://{args.host}:{args.port}/mcp"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"method": method, "args": payload},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(f"daemon unreachable at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise SystemExit(f"HTTP {resp.status_code}: {resp.text.strip()}")
    body = resp.json()
    if not body.get("ok"):
        raise SystemExit(f"error: {body.get('error', 'unknown')}")
    result = body.get("result", {})
    if not isinstance(result, dict):
        raise SystemExit("malformed response: result is not an object")
    return result


def print_json(result: dict[str, Any]) -> None:
    """Render an MCP response as indented JSON with optional Rich syntax.

    The bytes on a pipe are still ``json.dumps(..., indent=2)`` so
    downstream ``| jq`` keeps working; the TTY path picks up colour.
    """

    from rich.syntax import Syntax

    payload = json.dumps(result, indent=2)
    if console.is_terminal:
        console.print(Syntax(payload, "json", theme="ansi_dark", background_color="default"))
    else:
        console.print(payload, markup=False, highlight=False)


__all__ = [
    "add_http_args",
    "call",
    "print_json",
    "resolve_token",
]
