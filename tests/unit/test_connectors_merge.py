"""JsonConnector + TomlConnector merge/prune behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.connectors.base import (
    TESSERA_SERVER_NAME,
    McpServerSpec,
    UnsupportedConfigShapeError,
)
from tessera.connectors.file_safety import json_serialiser, read_json, read_toml, toml_serialiser
from tessera.connectors.json_connector import JsonConnector
from tessera.connectors.toml_connector import TomlConnector


def _spec() -> McpServerSpec:
    return McpServerSpec(
        url="http://127.0.0.1:5710/mcp",
        token="tessera_session_TESTTESTTESTTESTTESTTEST",
    )


# ---- JSON connector ------------------------------------------------------


@pytest.mark.unit
def test_json_apply_creates_config_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    result = connector.apply(path, _spec())
    assert result.no_op is False
    assert result.backup_path is None
    loaded = read_json(path)
    assert TESSERA_SERVER_NAME in loaded["mcpServers"]
    entry = loaded["mcpServers"][TESSERA_SERVER_NAME]
    assert entry["url"] == "http://127.0.0.1:5710/mcp"
    assert entry["headers"]["Authorization"].startswith("Bearer tessera_session_")


@pytest.mark.unit
def test_json_apply_preserves_other_servers(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_bytes(
        json_serialiser(
            {
                "mcpServers": {
                    "other-tool": {"command": "/usr/bin/other"},
                },
                "custom-user-key": "preserve me",
            }
        )
    )
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    result = connector.apply(path, _spec())
    assert result.backup_path is not None
    loaded = read_json(path)
    assert "other-tool" in loaded["mcpServers"]
    assert TESSERA_SERVER_NAME in loaded["mcpServers"]
    assert loaded["custom-user-key"] == "preserve me"


@pytest.mark.unit
def test_json_apply_refuses_invalid_shape(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text('{"mcpServers": "not a dict"}')
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    with pytest.raises(UnsupportedConfigShapeError):
        connector.apply(path, _spec())


@pytest.mark.unit
def test_json_apply_second_run_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    connector.apply(path, _spec())
    second = connector.apply(path, _spec())
    assert second.no_op is True
    assert second.backup_path is None


@pytest.mark.unit
def test_json_remove_preserves_other_servers(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_bytes(
        json_serialiser(
            {
                "mcpServers": {
                    "other-tool": {"command": "/usr/bin/other"},
                    TESSERA_SERVER_NAME: {"url": "http://127.0.0.1:5710/mcp"},
                },
            }
        )
    )
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    result = connector.remove(path)
    assert result.no_op is False
    loaded = read_json(path)
    assert TESSERA_SERVER_NAME not in loaded["mcpServers"]
    assert "other-tool" in loaded["mcpServers"]


@pytest.mark.unit
def test_json_remove_noop_when_tessera_absent(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_bytes(json_serialiser({"mcpServers": {"other": {}}}))
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    result = connector.remove(path)
    assert result.no_op is True


@pytest.mark.unit
def test_json_remove_noop_when_file_missing(tmp_path: Path) -> None:
    connector = JsonConnector(client_id="claude-desktop", display_name="Claude Desktop")
    result = connector.remove(tmp_path / "missing.json")
    assert result.no_op is True


# ---- TOML connector ------------------------------------------------------


@pytest.mark.unit
def test_toml_apply_preserves_unrelated_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(
        toml_serialiser(
            {
                "user": {"name": "tom"},
                "mcp_servers": {"other": {"url": "http://other"}},
            }
        )
    )
    connector = TomlConnector(client_id="codex", display_name="Codex")
    result = connector.apply(path, _spec())
    assert result.backup_path is not None
    loaded = read_toml(path)
    assert loaded["user"]["name"] == "tom"
    assert "other" in loaded["mcp_servers"]
    assert TESSERA_SERVER_NAME in loaded["mcp_servers"]


@pytest.mark.unit
def test_toml_remove_drops_empty_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(
        toml_serialiser(
            {
                "user": {"name": "tom"},
                "mcp_servers": {
                    TESSERA_SERVER_NAME: {"url": "http://127.0.0.1:5710/mcp"},
                },
            }
        )
    )
    connector = TomlConnector(client_id="codex", display_name="Codex")
    result = connector.remove(path)
    assert result.no_op is False
    loaded = read_toml(path)
    assert "mcp_servers" not in loaded
    assert loaded["user"]["name"] == "tom"


@pytest.mark.unit
def test_toml_remove_preserves_other_servers(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(
        toml_serialiser(
            {
                "mcp_servers": {
                    "other": {"url": "http://other"},
                    TESSERA_SERVER_NAME: {"url": "http://127.0.0.1:5710/mcp"},
                },
            }
        )
    )
    connector = TomlConnector(client_id="codex", display_name="Codex")
    connector.remove(path)
    loaded = read_toml(path)
    assert TESSERA_SERVER_NAME not in loaded["mcp_servers"]
    assert "other" in loaded["mcp_servers"]
