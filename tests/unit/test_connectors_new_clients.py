"""Connector wiring for the omp, pi, and OpenCode clients.

omp and pi reuse the default ``mcpServers`` JSON shape with the stdio
bridge entry; OpenCode uses a ``mcp`` top-level key with a
``{type, command, enabled}`` local entry. These tests pin the wired
registry behaviour (not just the bare JsonConnector mechanics).
"""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from tessera.connectors import available_clients, get_connector
from tessera.connectors.base import TESSERA_SERVER_NAME, McpServerSpec
from tessera.connectors.json_connector import JsonConnector


def _spec() -> McpServerSpec:
    return McpServerSpec(url="http://127.0.0.1:5710/mcp", token="tessera_session_abc123")


@pytest.mark.unit
def test_new_clients_are_registered() -> None:
    clients = available_clients()
    for client in ("omp", "pi", "opencode"):
        assert client in clients


@pytest.mark.unit
@pytest.mark.parametrize("client_id", ["omp", "pi"])
def test_omp_pi_write_stdio_bridge_entry(tmp_path: Path, client_id: str) -> None:
    connector = get_connector(client_id)
    assert isinstance(connector, JsonConnector)
    assert connector.top_level_key == "mcpServers"
    path = tmp_path / f"{client_id}.json"
    connector.apply(path, _spec())
    loaded = json.loads(path.read_text(encoding="utf-8"))
    entry = loaded["mcpServers"][TESSERA_SERVER_NAME]
    # Stdio bridge shape: command + args (no HTTP url/headers), matching
    # what omp/pi already use for archex.
    assert "command" in entry
    assert entry["args"][:3] == ["-m", "tessera.cli", "stdio"]
    assert "--url" in entry["args"]
    assert "--token" in entry["args"]
    assert "headers" not in entry


@pytest.mark.unit
@pytest.mark.parametrize("client_id", ["omp", "pi"])
def test_omp_pi_preserve_schema_and_existing_servers(tmp_path: Path, client_id: str) -> None:
    path = tmp_path / f"{client_id}.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "https://example/schema.json",
                "mcpServers": {"archex": {"command": "archex", "args": ["mcp"]}},
            }
        ),
        encoding="utf-8",
    )
    get_connector(client_id).apply(path, _spec())
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["$schema"] == "https://example/schema.json"
    assert "archex" in loaded["mcpServers"]
    assert TESSERA_SERVER_NAME in loaded["mcpServers"]


@pytest.mark.unit
def test_opencode_writes_under_mcp_key_with_local_shape(tmp_path: Path) -> None:
    connector = get_connector("opencode")
    assert isinstance(connector, JsonConnector)
    assert connector.top_level_key == "mcp"
    path = tmp_path / "opencode.json"
    connector.apply(path, _spec())
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" not in loaded
    entry = loaded["mcp"][TESSERA_SERVER_NAME]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"][1:4] == ["-m", "tessera.cli", "stdio"]
    assert "--url" in entry["command"]
    assert "--token" in entry["command"]


@pytest.mark.unit
def test_opencode_preserves_provider_and_other_mcp(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {"llamacpp": {"name": "Local"}},
                "model": "llamacpp/local-model",
                "mcp": {
                    "git-tools": {
                        "type": "local",
                        "command": ["uvx", "mcp-server-git"],
                        "enabled": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    get_connector("opencode").apply(path, _spec())
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["provider"]["llamacpp"]["name"] == "Local"
    assert loaded["model"] == "llamacpp/local-model"
    assert "git-tools" in loaded["mcp"]
    assert TESSERA_SERVER_NAME in loaded["mcp"]


@pytest.mark.unit
def test_opencode_remove_prunes_only_tessera(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    connector = get_connector("opencode")
    path.write_text(
        json.dumps({"mcp": {"git-tools": {"type": "local", "command": ["uvx", "mcp-server-git"]}}}),
        encoding="utf-8",
    )
    connector.apply(path, _spec())
    connector.remove(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert "git-tools" in loaded["mcp"]
    assert TESSERA_SERVER_NAME not in loaded["mcp"]


@pytest.mark.unit
def test_new_client_default_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if platform.system() not in ("Darwin", "Linux", "Windows"):
        pytest.skip("unsupported platform")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert get_connector("omp").default_path() == tmp_path / ".omp" / "agent" / "mcp.json"
    assert get_connector("pi").default_path() == tmp_path / ".pi" / "agent" / "mcp.json"
    assert (
        get_connector("opencode").default_path()
        == tmp_path / ".config" / "opencode" / "opencode.json"
    )


@pytest.mark.unit
def test_opencode_honors_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert get_connector("opencode").default_path() == xdg / "opencode" / "opencode.json"
