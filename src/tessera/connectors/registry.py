"""Client-id → connector lookup.

Keeps the five v0.1 clients in one place so the CLI dispatcher and
the help text stay in sync. New connectors land by adding one entry
here.
"""

from __future__ import annotations

from tessera.connectors.base import (
    Connector,
    UnknownClientError,
    build_stdio_via_mcp_remote_entry,
)
from tessera.connectors.chatgpt import ChatGptConnector
from tessera.connectors.json_connector import (
    JsonConnector,
    claude_code_paths,
    claude_desktop_paths,
    cursor_paths,
)
from tessera.connectors.toml_connector import TomlConnector, codex_paths


def _build_connectors() -> dict[str, Connector]:
    registry: dict[str, Connector] = {}
    # Claude Desktop's MCP loader speaks stdio only; the stdio-via-
    # mcp-remote builder emits an npx command that tunnels stdio ↔
    # HTTP. Every other JSON-based client (Claude Code, Cursor) speaks
    # HTTP MCP natively and uses the default native-HTTP entry builder.
    registry["claude-desktop"] = JsonConnector(
        client_id="claude-desktop",
        display_name="Claude Desktop",
        paths=claude_desktop_paths(),
        entry_builder=build_stdio_via_mcp_remote_entry,
    )
    registry["claude-code"] = JsonConnector(
        client_id="claude-code",
        display_name="Claude Code",
        paths=claude_code_paths(),
    )
    registry["cursor"] = JsonConnector(
        client_id="cursor",
        display_name="Cursor",
        paths=cursor_paths(),
    )
    registry["codex"] = TomlConnector(
        client_id="codex",
        display_name="Codex",
        paths=codex_paths(),
    )
    registry["chatgpt"] = ChatGptConnector()
    return registry


_REGISTRY: dict[str, Connector] = _build_connectors()


def available_clients() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_connector(client_id: str) -> Connector:
    try:
        return _REGISTRY[client_id]
    except KeyError as exc:
        raise UnknownClientError(
            f"unknown client {client_id!r}; supported: {', '.join(available_clients())}"
        ) from exc


__all__ = ["available_clients", "get_connector"]
