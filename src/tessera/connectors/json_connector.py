"""JSON-based MCP config writers (Claude Desktop, Claude Code, Cursor).

All three clients share the same on-disk convention: a JSON document
with an ``mcpServers`` object keyed by server name. They differ only
in default path. This connector is parameterised on path + display
name so every JSON-based client shares the same merge, backup, and
atomic-replace machinery.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tessera.connectors.base import (
    TESSERA_SERVER_NAME,
    ConnectorResult,
    McpServerSpec,
    UnsupportedConfigShapeError,
    build_server_entry,
    token_from_entry,
)
from tessera.connectors.file_safety import (
    WriteOutcome,
    json_serialiser,
    read_json,
    write_safely,
)

EntryBuilder = Callable[[McpServerSpec], Mapping[str, object]]

_TOP_LEVEL_KEY = "mcpServers"

PathResolver = Callable[[], Path]


@dataclass(frozen=True, slots=True)
class JsonConnector:
    """A JSON-based connector parameterised on path resolution.

    ``paths`` maps ``platform.system()`` values ("Darwin", "Linux",
    "Windows") to a callable that returns the default config path on
    that OS. The callable form (rather than a static Path) lets the
    resolver pick up ``$HOME`` / ``$APPDATA`` at call time, so the
    connector works correctly under tests that monkeypatch ``HOME``.
    """

    client_id: str
    display_name: str
    paths: Mapping[str, PathResolver] = field(default_factory=dict)
    # Per-client entry builder. Defaults to the native HTTP-MCP shape
    # used by Claude Code, Cursor, and Codex. Claude Desktop overrides
    # with ``build_stdio_via_mcp_remote_entry`` because its MCP loader
    # speaks stdio transport only.
    entry_builder: EntryBuilder = build_server_entry
    # Top-level config key holding the MCP server map: ``mcpServers``
    # for Claude/Cursor/omp/pi, ``mcp`` for OpenCode.
    top_level_key: str = _TOP_LEVEL_KEY

    def default_path(self) -> Path:
        resolver = self.paths.get(platform.system())
        if resolver is None:
            raise UnsupportedConfigShapeError(
                f"{self.display_name}: no default config path registered for "
                f"{platform.system()!r}; pass --path"
            )
        return resolver()

    def apply(self, path: Path, server: McpServerSpec) -> ConnectorResult:
        existing = read_json(path)
        merged = _merge_entry(existing, server, self.entry_builder, self.top_level_key)
        outcome = write_safely(path, merged, serialiser=json_serialiser)
        return _to_result(outcome)

    def read_token(self, path: Path) -> str | None:
        if not path.exists():
            return None
        servers = read_json(path).get(self.top_level_key)
        if not isinstance(servers, dict):
            return None
        return token_from_entry(servers.get(TESSERA_SERVER_NAME))

    def remove(self, path: Path) -> ConnectorResult:
        if not path.exists():
            # A disconnect against a missing file is a no-op by design —
            # the user may have already removed the Tessera entry by
            # hand, and overwriting with an empty config would stomp
            # sibling settings that a stale cached file might carry.
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        existing = read_json(path)
        if not _has_tessera_entry(existing, self.top_level_key):
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        pruned = _prune_entry(existing, self.top_level_key)
        outcome = write_safely(path, pruned, serialiser=json_serialiser)
        return _to_result(outcome)


# ---- Path resolvers ------------------------------------------------------


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _windows_appdata() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # Windows without %APPDATA% is a broken install, not a
        # configurable state — surface it explicitly rather than
        # silently picking a wrong fallback that would scatter
        # config files across the user's disk.
        raise UnsupportedConfigShapeError(
            "APPDATA environment variable is not set; cannot resolve "
            "the Windows config path. Pass --path explicitly."
        )
    return Path(appdata)


def claude_desktop_paths() -> dict[str, PathResolver]:
    return {
        "Darwin": lambda: (
            _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        ),
        "Linux": lambda: _home() / ".config" / "Claude" / "claude_desktop_config.json",
        "Windows": lambda: _windows_appdata() / "Claude" / "claude_desktop_config.json",
    }


def claude_code_paths() -> dict[str, PathResolver]:
    # Claude Code reads MCP servers from ~/.claude.json under a
    # top-level ``mcpServers`` key. Everything else under ~/.claude/
    # (agents, backups, commands, caches) is runtime artifacts — not
    # config — and Claude Code ignores any files written there.
    #
    # Earlier versions of this connector wrote to
    # ~/.claude/claude_code_config.json, which Claude Code silently
    # ignored. The fix points at the real single-file location; the
    # shared ``_merge_entry`` preserves every other top-level key so
    # the user's existing Claude Code settings (tipsHistory, usage
    # counters, UI flags, etc.) are untouched.
    return {
        "Darwin": lambda: _home() / ".claude.json",
        "Linux": lambda: _home() / ".claude.json",
        "Windows": lambda: _home() / ".claude.json",
    }


def cursor_paths() -> dict[str, PathResolver]:
    return {
        "Darwin": lambda: _home() / ".cursor" / "mcp.json",
        "Linux": lambda: _home() / ".cursor" / "mcp.json",
        "Windows": lambda: _home() / ".cursor" / "mcp.json",
    }


def omp_paths() -> dict[str, PathResolver]:
    # Oh My Pi reads MCP servers from ~/.omp/agent/mcp.json under a
    # top-level ``mcpServers`` key with stdio ``{command, args}`` entries.
    return {
        "Darwin": lambda: _home() / ".omp" / "agent" / "mcp.json",
        "Linux": lambda: _home() / ".omp" / "agent" / "mcp.json",
        "Windows": lambda: _home() / ".omp" / "agent" / "mcp.json",
    }


def pi_paths() -> dict[str, PathResolver]:
    # Pi (the oh-my-pi base CLI) mirrors omp: ~/.pi/agent/mcp.json,
    # top-level ``mcpServers`` with stdio ``{command, args}`` entries.
    return {
        "Darwin": lambda: _home() / ".pi" / "agent" / "mcp.json",
        "Linux": lambda: _home() / ".pi" / "agent" / "mcp.json",
        "Windows": lambda: _home() / ".pi" / "agent" / "mcp.json",
    }


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else _home() / ".config"


def opencode_paths() -> dict[str, PathResolver]:
    # OpenCode reads global config from $XDG_CONFIG_HOME/opencode/opencode.json
    # (default ~/.config/opencode/opencode.json) under a top-level ``mcp``
    # key with ``{type, command|url, enabled}`` entries.
    return {
        "Darwin": lambda: _xdg_config_home() / "opencode" / "opencode.json",
        "Linux": lambda: _xdg_config_home() / "opencode" / "opencode.json",
        "Windows": lambda: _xdg_config_home() / "opencode" / "opencode.json",
    }


# ---- Merge helpers -------------------------------------------------------


def _merge_entry(
    existing: dict[str, object],
    server: McpServerSpec,
    entry_builder: EntryBuilder,
    top_level_key: str = _TOP_LEVEL_KEY,
) -> dict[str, object]:
    """Return a copy of ``existing`` with the Tessera entry merged in.

    ``existing`` is not mutated. When the file already has a
    ``top_level_key`` object, its other keys are preserved as-is; only
    the ``tessera`` entry is rewritten. When the slot exists but isn't a
    dict, the merge raises :class:`UnsupportedConfigShapeError` rather
    than stomping it. ``entry_builder`` produces the per-entry payload;
    ``top_level_key`` is ``mcpServers`` for most clients and ``mcp`` for
    OpenCode.
    """

    merged = dict(existing)
    servers_raw = merged.get(top_level_key, {})
    if not isinstance(servers_raw, dict):
        raise UnsupportedConfigShapeError(
            f"config has {top_level_key!r} = {type(servers_raw).__name__}; expected a JSON object"
        )
    servers = dict(servers_raw)
    servers[TESSERA_SERVER_NAME] = dict(entry_builder(server))
    merged[top_level_key] = servers
    return merged


def _has_tessera_entry(existing: dict[str, object], top_level_key: str = _TOP_LEVEL_KEY) -> bool:
    servers = existing.get(top_level_key)
    return isinstance(servers, dict) and TESSERA_SERVER_NAME in servers


def _prune_entry(
    existing: dict[str, object], top_level_key: str = _TOP_LEVEL_KEY
) -> dict[str, object]:
    """Return a copy of ``existing`` with the Tessera entry removed.

    If removing Tessera empties the server map, the empty map is
    preserved — an emptied key is still a valid shape and the user's
    config-management scripts may expect the key to exist.
    """

    pruned = dict(existing)
    servers_raw = pruned.get(top_level_key, {})
    if not isinstance(servers_raw, dict):
        return pruned
    servers = {k: v for k, v in servers_raw.items() if k != TESSERA_SERVER_NAME}
    pruned[top_level_key] = servers
    return pruned


def _to_result(outcome: WriteOutcome) -> ConnectorResult:
    return ConnectorResult(
        path=outcome.path,
        backup_path=outcome.backup_path,
        no_op=outcome.already_matches,
    )


__all__ = [
    "JsonConnector",
    "PathResolver",
    "claude_code_paths",
    "claude_desktop_paths",
    "cursor_paths",
    "omp_paths",
    "opencode_paths",
    "pi_paths",
]
