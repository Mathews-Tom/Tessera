"""Connector protocol — the per-client contract for ``tessera connect``.

A connector is the module that knows one AI tool's config-file shape.
Every connector implements the same three-method protocol:

* ``default_path()``  — where this client looks for its MCP config.
* ``apply(path, server)`` — add the Tessera MCP entry.
* ``remove(path)`` — remove the Tessera MCP entry; leave the rest alone.

The shared shape across every v0.1 client is "an MCP server registry
keyed by server name". JSON clients (Claude Desktop, Claude Code,
Cursor) nest it under ``mcpServers``; TOML clients (Codex) use
``[mcp_servers.tessera]``. The connector hides the format difference
so the caller only supplies the transport details.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# The MCP server name every Tessera connector uses. Hardcoded rather
# than configurable so ``disconnect`` can find the entry it wrote
# regardless of which binary did the writing.
TESSERA_SERVER_NAME = "tessera"


class ConnectorError(Exception):
    """Base class for connector failures."""


class UnknownClientError(ConnectorError):
    """Caller passed an id that isn't in the connector registry."""


class UnsupportedConfigShapeError(ConnectorError):
    """Config file exists but its shape is not one we can safely merge."""


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """The transport details a Tessera MCP entry carries.

    ``url`` is the HTTP endpoint (``http://127.0.0.1:5710/mcp``). ``token``
    is the raw bearer the client presents on every request. Connectors
    translate this into the per-client config shape — URL field names,
    header-vs-url-param conventions, HTTP-vs-stdio transport flags — but
    the upstream shape is always these two fields.
    """

    url: str
    token: str


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    """What a connector reports back to ``tessera connect`` / ``disconnect``.

    ``backup_path`` is None when the file did not pre-exist. ``no_op`` is
    True when the merge produced identical bytes (e.g. re-running
    ``connect`` after a successful connect). Callers render these fields
    verbatim in the CLI output so the user knows whether the operation
    changed anything and where the backup landed.
    """

    path: Path
    backup_path: Path | None
    no_op: bool


class Connector(Protocol):
    """One client's MCP-config writer.

    ``default_path`` resolves the platform-specific default config
    location (macOS / Linux / Windows). Callers may override with an
    explicit path (useful for tests and for users whose config lives
    in a non-default location). ``client_id`` and ``display_name`` are
    read-only because concrete implementations are frozen dataclasses.
    """

    @property
    def client_id(self) -> str:
        """Short kebab-case id used on the CLI (``claude-desktop``, ``codex``)."""

    @property
    def display_name(self) -> str:
        """Human-readable name used in CLI output."""

    def default_path(self) -> Path: ...

    def apply(self, path: Path, server: McpServerSpec) -> ConnectorResult: ...

    def remove(self, path: Path) -> ConnectorResult: ...


def build_server_entry(server: McpServerSpec) -> Mapping[str, object]:
    """Return the per-entry payload for clients that speak HTTP MCP natively.

    Claude Code, Cursor, and Codex all accept the MCP spec's native
    HTTP transport shape: ``{"type": "http", "url": ..., "headers": ...}``.
    Claude Desktop is the exception — see
    :func:`build_stdio_via_mcp_remote_entry` for its stdio-bridge
    equivalent.

    ChatGPT Dev Mode uses a different shape entirely (no config file,
    URL-embedded bootstrap nonce) handled by its own connector.
    """

    return {
        "type": "http",
        "url": server.url,
        "headers": {
            "Authorization": f"Bearer {server.token}",
        },
    }


def build_stdio_via_tessera_bridge_entry(server: McpServerSpec) -> Mapping[str, object]:
    """Return a stdio entry that bridges via Tessera's built-in ``stdio`` command.

    Claude Desktop's MCP loader supports stdio transport only. Tessera
    ships a first-party stdio ↔ HTTP bridge (``tessera stdio --url X
    --token Y``) that does exactly this translation. It replaces the
    earlier ``mcp-remote`` approach because current mcp-remote
    versions enforce OAuth 2.0 dynamic client registration before a
    Bearer token is accepted — Tessera's capability-token model is
    not OAuth, so mcp-remote's registration attempt 500s against the
    daemon. Adding OAuth to the daemon is v0.3+ scope.

    The invocation uses ``sys.executable -m tessera.cli`` rather than
    the ``tessera`` script shim because Claude Desktop's spawn
    environment does not inherit the user's shell ``PATH``. Resolving
    the Python interpreter via :data:`sys.executable` at config-write
    time pins the bridge to the Tessera install that minted the token,
    which is the install that speaks to the running daemon.
    """

    # stdlib import kept local so this module does not pull sys at
    # cold-path import time.
    import sys

    return {
        "command": sys.executable,
        "args": [
            "-m",
            "tessera.cli",
            "stdio",
            "--url",
            server.url,
            "--token",
            server.token,
        ],
    }


# Backwards-compat alias. Callers upgrading across v0.1.x keep working
# while the rename propagates.
build_stdio_via_mcp_remote_entry = build_stdio_via_tessera_bridge_entry


__all__ = [
    "TESSERA_SERVER_NAME",
    "Connector",
    "ConnectorError",
    "ConnectorResult",
    "McpServerSpec",
    "UnknownClientError",
    "UnsupportedConfigShapeError",
    "build_server_entry",
    "build_stdio_via_mcp_remote_entry",
    "build_stdio_via_tessera_bridge_entry",
]
