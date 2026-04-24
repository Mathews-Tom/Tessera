"""Stdio ↔ HTTP MCP bridge for clients that only speak stdio (Claude Desktop).

Claude Desktop's MCP loader accepts stdio transport only. Tessera's
daemon exposes a custom JSON-RPC-ish shape over HTTP (``{"method": X,
"args": Y}`` → ``{"ok": bool, "result"?: ..., "error"?: ...}``) rather
than the canonical MCP Streamable HTTP protocol, because the custom
shape pre-dates the MCP wire protocol stabilising and carries the
``Authorization: Bearer <capability-token>`` discipline straight.

This bridge does two things:

1. Serves a standard MCP stdio server on the parent's stdin / stdout
   using :mod:`mcp.server.lowlevel.server`, so Claude Desktop sees a
   normal stdio MCP server.
2. Translates every ``tools/list`` and ``tools/call`` into a plain
   ``POST`` against the Tessera daemon's ``/mcp`` endpoint with the
   Tessera-native envelope, then wraps the response back into MCP
   ``TextContent``.

The tool surface is generated from
``tessera.mcp_surface.tools.MCP_TOOL_CONTRACTS`` so Claude Desktop's
startup schema and the daemon's validation contract stay in sync.

Environment variables:
- ``TESSERA_STDIO_BRIDGE_DEBUG=1`` — print full traceback to stderr
  on failure. Useful when Claude Desktop reports "Server disconnected".
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from tessera.mcp_surface.tools import MCP_TOOL_CONTRACTS

_TOOLS: tuple[Tool, ...] = tuple(
    Tool(
        name=contract.name,
        description=contract.description,
        inputSchema=contract.input_schema,
    )
    for contract in MCP_TOOL_CONTRACTS
)


async def _call_tessera(
    client: httpx.AsyncClient, url: str, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """POST one tool call to the daemon and unwrap the Tessera envelope.

    Raises :class:`RuntimeError` on a non-200 response or a failed
    payload so the error bubbles up through the stdio server and
    Claude Desktop renders a tool-call failure rather than a silent
    empty result.
    """

    resp = await client.post(url, json={"method": name, "args": args})
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"daemon returned non-object body: {body!r}")
    if not body.get("ok"):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code", "unknown")
            message = error.get("message", "")
            raise RuntimeError(f"{code}: {message}".rstrip())
        raise RuntimeError(error or "daemon returned error without detail")
    result = body.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError(f"daemon returned non-object result: {result!r}")
    return result


async def _run_bridge(url: str, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=30.0))
        server: Server[object] = Server("tessera")

        # The mcp SDK's decorator API does not ship full mypy stubs;
        # the decorators are typed ``Any -> Any`` so mypy --strict
        # flags them as untyped-decorator. Suppress the warnings at
        # the call site rather than lowering mypy's strictness globally.
        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def _list_tools() -> list[Tool]:
            return list(_TOOLS)

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            import json as _json

            result = await _call_tessera(client, url, name, arguments)
            # Serialise the result as a single JSON TextContent. Claude
            # Desktop renders JSON content blocks inline, which is
            # what the user already sees from other Tessera-native
            # MCP clients.
            return [TextContent(type="text", text=_json.dumps(result, indent=2))]

        downstream_read, downstream_write = await stack.enter_async_context(stdio_server())
        await server.run(
            downstream_read,
            downstream_write,
            server.create_initialization_options(),
        )

    return 0


def run(url: str, token: str) -> int:
    """Synchronous entry point — dispatch to asyncio and surface exceptions.

    ``TESSERA_STDIO_BRIDGE_DEBUG=1`` prints a full traceback before the
    exit. Useful when Claude Desktop reports "Server disconnected" and
    the operator needs to see the underlying cause.
    """

    try:
        return asyncio.run(_run_bridge(url, token))
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:  # top-level boundary: classify + exit non-zero
        if os.environ.get("TESSERA_STDIO_BRIDGE_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        print(
            f"tessera stdio bridge failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


def run_stub() -> int:
    """Compatibility shim for older callers that imported the P14 stub.

    The stub returned a structured refusal; the real bridge needs
    ``--url`` and ``--token``. Callers should go through the CLI
    (``tessera stdio --url ... --token ...``) which invokes
    :func:`run` directly.
    """

    print(
        "tessera stdio bridge requires --url and --token; see `tessera stdio --help`.",
        file=sys.stderr,
    )
    return 2


__all__ = ["run", "run_stub"]
