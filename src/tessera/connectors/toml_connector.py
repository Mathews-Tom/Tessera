"""TOML-based MCP config writer (Codex).

Codex's MCP config lives at ``~/.codex/config.toml`` under the table
``[mcp_servers.tessera]``. The shape mirrors the JSON connectors — a
dict-of-dicts keyed by server name — but rides on TOML, so comments
and trailing-newline conventions are not round-trip preserved. The
module docstring in ``file_safety.toml_serialiser`` calls that out;
this module honours it by writing the whole file from the parsed
structure, which means a user who had free-form comments between
tables will see them dropped on first ``tessera connect``. The
pre-write backup preserves the original.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
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
    read_toml,
    toml_serialiser,
    write_safely,
)
from tessera.connectors.json_connector import PathResolver

_TOP_LEVEL_KEY = "mcp_servers"


@dataclass(frozen=True, slots=True)
class TomlConnector:
    """A TOML-based connector parameterised on path resolution.

    Mirrors :class:`~tessera.connectors.json_connector.JsonConnector` so
    the CLI layer can delegate to either without branching on format.
    The top-level key differs (``mcp_servers`` in TOML vs
    ``mcpServers`` in JSON) — this is Codex's convention, not an
    accident.
    """

    client_id: str
    display_name: str
    paths: Mapping[str, PathResolver] = field(default_factory=dict)

    def default_path(self) -> Path:
        resolver = self.paths.get(platform.system())
        if resolver is None:
            raise UnsupportedConfigShapeError(
                f"{self.display_name}: no default config path registered for "
                f"{platform.system()!r}; pass --path"
            )
        return resolver()

    def apply(self, path: Path, server: McpServerSpec) -> ConnectorResult:
        existing = read_toml(path)
        merged = _merge_entry(existing, server)
        outcome = write_safely(path, merged, serialiser=toml_serialiser)
        return _to_result(outcome)

    def remove(self, path: Path) -> ConnectorResult:
        if not path.exists():
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        existing = read_toml(path)
        if not _has_tessera_entry(existing):
            return ConnectorResult(path=path, backup_path=None, no_op=True)
        pruned = _prune_entry(existing)
        outcome = write_safely(path, pruned, serialiser=toml_serialiser)
        return _to_result(outcome)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def codex_paths() -> dict[str, PathResolver]:
    return {
        "Darwin": lambda: _home() / ".codex" / "config.toml",
        "Linux": lambda: _home() / ".codex" / "config.toml",
        "Windows": lambda: _home() / ".codex" / "config.toml",
    }


def _merge_entry(existing: dict[str, object], server: McpServerSpec) -> dict[str, object]:
    merged = dict(existing)
    servers_raw = merged.get(_TOP_LEVEL_KEY, {})
    if not isinstance(servers_raw, dict):
        raise UnsupportedConfigShapeError(
            f"config has {_TOP_LEVEL_KEY!r} = {type(servers_raw).__name__}; expected a TOML table"
        )
    servers = dict(servers_raw)
    servers[TESSERA_SERVER_NAME] = dict(build_server_entry(server))
    merged[_TOP_LEVEL_KEY] = servers
    return merged


def _has_tessera_entry(existing: dict[str, object]) -> bool:
    servers = existing.get(_TOP_LEVEL_KEY)
    return isinstance(servers, dict) and TESSERA_SERVER_NAME in servers


def _prune_entry(existing: dict[str, object]) -> dict[str, object]:
    pruned = dict(existing)
    servers_raw = pruned.get(_TOP_LEVEL_KEY, {})
    if not isinstance(servers_raw, dict):
        return pruned
    servers = {k: v for k, v in servers_raw.items() if k != TESSERA_SERVER_NAME}
    # tomli-w's encoder rejects empty tables at the top level under
    # certain call paths. Preserve the key only when it still has
    # sibling entries; otherwise drop it to keep the config
    # round-trip stable.
    if servers:
        pruned[_TOP_LEVEL_KEY] = servers
    else:
        pruned.pop(_TOP_LEVEL_KEY, None)
    return pruned


def _to_result(outcome: WriteOutcome) -> ConnectorResult:
    return ConnectorResult(
        path=outcome.path,
        backup_path=outcome.backup_path,
        no_op=outcome.already_matches,
    )


__all__ = ["TomlConnector", "codex_paths"]
