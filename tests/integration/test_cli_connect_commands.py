"""``tessera connect`` / ``tessera disconnect`` end-to-end for file-based clients.

Runs the real CLI handlers against a fixture vault, verifies the
connector writes the expected MCP-server entry, re-runs to confirm
idempotence, and then disconnects. The ChatGPT Dev Mode URL-exchange
flow is covered separately in
``tests/security/test_exchange_endpoint.py`` because it needs a live
HTTP server, not a file-writer.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tessera.cli.__main__ import _build_parser
from tessera.connectors.base import TESSERA_SERVER_NAME, ConnectorResult, McpServerSpec
from tessera.connectors.json_connector import JsonConnector


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tess_", dir="/tmp") as tmp:
        yield Path(tmp)


def _init_vault(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "correct horse battery staple")
    vault = short_tmp / "v.db"
    parser = _build_parser()
    init_args = parser.parse_args(["init", "--vault", str(vault), "--agent-name", "default"])
    init_rc = init_args.handler(init_args)
    assert init_rc == 0
    # tessera init emits "agent_id: <int>" in its output; parse it so
    # the connect test does not need to reach into vault internals.
    # Fall back to the first agent in the agents table if the format
    # changes — this keeps the test resilient to cosmetic tweaks.
    import sqlcipher3

    from tessera.vault.encryption import derive_key, load_salt

    salt = load_salt(vault)
    with derive_key(bytearray(b"correct horse battery staple"), salt) as key:
        conn = sqlcipher3.connect(str(vault), isolation_level=None)
        try:
            conn.execute(f"PRAGMA key = {key.as_pragma_literal()}")
            agent_id = int(conn.execute("SELECT id FROM agents LIMIT 1").fetchone()[0])
        finally:
            conn.close()
    return vault, agent_id


@pytest.mark.integration
def test_connect_claude_desktop_writes_entry(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "claude_desktop_config.json"
    parser = _build_parser()

    connect_args = parser.parse_args(
        [
            "connect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )
    rc = connect_args.handler(connect_args)
    assert rc == 0
    assert config_path.exists()
    loaded = json.loads(config_path.read_text())
    assert TESSERA_SERVER_NAME in loaded["mcpServers"]
    entry = loaded["mcpServers"][TESSERA_SERVER_NAME]
    # Claude Desktop's MCP loader speaks stdio transport only. The
    # connector now emits a ``python -m tessera.cli stdio`` invocation
    # that runs Tessera's first-party bridge. The shape has been
    # through three bugs on this PR: `"transport": "http"` (silently
    # ignored), `"type": "http"` (rejected because Claude Desktop
    # expects stdio), and `npx mcp-remote` (OAuth enforcement
    # incompatible with capability tokens). This assertion regression-
    # guards the stdio-via-tessera-bridge form.
    import sys as _sys

    assert entry["command"] == _sys.executable
    assert entry["args"][:3] == ["-m", "tessera.cli", "stdio"]
    # URL arrives as a named `--url` flag.
    url_idx = entry["args"].index("--url") + 1
    assert entry["args"][url_idx].startswith("http://127.0.0.1:")
    # Token arrives as a named `--token` flag.
    token_idx = entry["args"].index("--token") + 1
    assert entry["args"][token_idx].startswith("tessera_service_")
    # No npx, no mcp-remote, no native-HTTP keys.
    assert "npx" not in entry["args"]
    assert "mcp-remote" not in entry["args"]
    assert "type" not in entry
    assert "transport" not in entry
    assert "url" not in entry
    out = capsys.readouterr().out
    assert "wrote Tessera entry" in out
    from tessera.vault.connection import VaultConnection
    from tessera.vault.encryption import derive_key, load_salt

    salt = load_salt(vault)
    with (
        derive_key(bytearray(b"correct horse battery staple"), salt) as key,
        VaultConnection.open(vault, key) as vc,
    ):
        registration = vc.connection.execute(
            "SELECT connector_id, config_path, agent_id, active_capability_id, "
            "pending_capability_id, access_ttl_seconds "
            "FROM managed_connector_installations"
        ).fetchone()
    assert registration is not None
    assert registration[0] == "claude-desktop"
    assert registration[1] == str(config_path.resolve())
    assert registration[2] == agent_id
    assert registration[3] > 0
    assert registration[4] is None
    assert registration[5] == 90 * 24 * 60 * 60


@pytest.mark.integration
def test_connect_write_failure_does_not_register_or_leave_capability_live(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "cursor.json"
    parser = _build_parser()
    connect_args = parser.parse_args(
        [
            "connect",
            "cursor",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )

    def _raise_write_error(
        self: JsonConnector, path: Path, server: McpServerSpec
    ) -> ConnectorResult:
        del self, path, server
        raise OSError("forced config write failure")

    monkeypatch.setattr(JsonConnector, "apply", _raise_write_error)
    assert connect_args.handler(connect_args) == 1

    from tessera.vault.connection import VaultConnection
    from tessera.vault.encryption import derive_key, load_salt

    salt = load_salt(vault)
    with (
        derive_key(bytearray(b"correct horse battery staple"), salt) as key,
        VaultConnection.open(vault, key) as vc,
    ):
        registrations = vc.connection.execute(
            "SELECT COUNT(*) FROM managed_connector_installations"
        ).fetchone()
        live_capabilities = vc.connection.execute(
            "SELECT COUNT(*) FROM capabilities WHERE revoked_at IS NULL"
        ).fetchone()
    assert registrations[0] == 0
    assert live_capabilities[0] == 0
    assert not config_path.exists()


@pytest.mark.integration
def test_connect_claude_code_uses_native_http(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Claude Code speaks HTTP MCP natively and takes the `type: http`
    # entry shape. Regression guard against accidentally emitting the
    # stdio-via-mcp-remote wrapper (which would add an `npx` dep for
    # no reason on a client that doesn't need it).
    vault, agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "claude_code.json"
    parser = _build_parser()
    connect_args = parser.parse_args(
        [
            "connect",
            "claude-code",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )
    assert connect_args.handler(connect_args) == 0
    loaded = json.loads(config_path.read_text())
    entry = loaded["mcpServers"][TESSERA_SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"].startswith("http://127.0.0.1:")
    assert entry["headers"]["Authorization"].startswith("Bearer tessera_service_")
    # Explicit: Claude Code does NOT get the mcp-remote stdio wrapper.
    assert "command" not in entry
    assert "args" not in entry


@pytest.mark.integration
def test_connect_is_idempotent(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "mcp.json"
    parser = _build_parser()
    first = parser.parse_args(
        [
            "connect",
            "cursor",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )
    first.handler(first)
    capsys.readouterr()  # discard
    # Second run mints a fresh token → config bytes differ → the
    # writer takes a backup rather than reporting no-op. This is
    # the correct behaviour: re-running connect rotates the token.
    second = parser.parse_args(
        [
            "connect",
            "cursor",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )
    rc = second.handler(second)
    assert rc == 0
    backups = list(config_path.parent.glob(f"{config_path.name}.tessera-backup-*"))
    assert backups, "expected a backup on re-connect"


@pytest.mark.integration
def test_disconnect_removes_tessera_entry(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "claude_desktop_config.json"
    parser = _build_parser()
    connect_args = parser.parse_args(
        [
            "connect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--agent-id",
            str(agent_id),
            "--path",
            str(config_path),
        ]
    )
    connect_args.handler(connect_args)
    capsys.readouterr()
    disconnect_args = parser.parse_args(
        [
            "disconnect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--path",
            str(config_path),
        ]
    )
    rc = disconnect_args.handler(disconnect_args)
    assert rc == 0
    loaded = json.loads(config_path.read_text())
    assert TESSERA_SERVER_NAME not in loaded.get("mcpServers", {})
    out = capsys.readouterr().out
    assert "removed Tessera entry" in out


@pytest.mark.integration
def test_disconnect_on_missing_config_is_no_op(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, _agent_id = _init_vault(short_tmp, monkeypatch)
    config_path = short_tmp / "nonexistent.json"
    parser = _build_parser()
    args = parser.parse_args(
        [
            "disconnect",
            "claude-desktop",
            "--vault",
            str(vault),
            "--path",
            str(config_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no Tessera entry" in out


@pytest.mark.integration
def test_disconnect_chatgpt_instructs_token_revoke(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, _agent_id = _init_vault(short_tmp, monkeypatch)
    parser = _build_parser()
    args = parser.parse_args(["disconnect", "chatgpt", "--vault", str(vault)])
    rc = args.handler(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "tessera tokens revoke" in out
