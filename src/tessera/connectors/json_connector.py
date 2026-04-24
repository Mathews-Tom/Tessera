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
        merged = _merge_entry(existing, server, self.entry_builder)
        outcome = write_safely(path, merged, serialiser=json_serialiser)
        return _to_result(outcome)

    def remove(self, path: Path) -> ConnectorResult:
        if not path.exists():
            # A disconnect against a missing file is a no-op by design —
            # the user may have already removed the Tessera entry by
            # hand, and overwriting with an empty config would stomp
            # sibling settings that a stale cached file might carry.
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        existing = read_json(path)
        if not _has_tessera_entry(existing):
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        pruned = _prune_entry(existing)
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


# ---- Merge helpers -------------------------------------------------------


def _merge_entry(
    existing: dict[str, object],
    server: McpServerSpec,
    entry_builder: EntryBuilder,
) -> dict[str, object]:
    """Return a copy of ``existing`` with the Tessera entry merged in.

    ``existing`` is not mutated. When the file already has a
    ``mcpServers`` object, its other keys are preserved as-is; only
    ``mcpServers["tessera"]`` is rewritten. When the top-level
    ``mcpServers`` slot exists but isn't a dict, the merge raises
    :class:`UnsupportedConfigShapeError` rather than stomping it.

    ``entry_builder`` produces the per-entry payload; pluggable so
    Claude Desktop's stdio-via-mcp-remote shape and the native HTTP
    shape can share the rest of the merge machinery.
    """

    merged = dict(existing)
    servers_raw = merged.get(_TOP_LEVEL_KEY, {})
    if not isinstance(servers_raw, dict):
        raise UnsupportedConfigShapeError(
            f"config has {_TOP_LEVEL_KEY!r} = {type(servers_raw).__name__}; expected a JSON object"
        )
    servers = dict(servers_raw)
    servers[TESSERA_SERVER_NAME] = dict(entry_builder(server))
    merged[_TOP_LEVEL_KEY] = servers
    return merged


def _has_tessera_entry(existing: dict[str, object]) -> bool:
    servers = existing.get(_TOP_LEVEL_KEY)
    return isinstance(servers, dict) and TESSERA_SERVER_NAME in servers


def _prune_entry(existing: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``existing`` with the Tessera entry removed.

    If removing Tessera empties the ``mcpServers`` map, the empty map
    is preserved — an emptied key is still a valid JSON shape and the
    user's config-management scripts may expect the key to exist.
    Deleting it would be a surprise.
    """

    pruned = dict(existing)
    servers_raw = pruned.get(_TOP_LEVEL_KEY, {})
    if not isinstance(servers_raw, dict):
        return pruned
    servers = {k: v for k, v in servers_raw.items() if k != TESSERA_SERVER_NAME}
    pruned[_TOP_LEVEL_KEY] = servers
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
]
