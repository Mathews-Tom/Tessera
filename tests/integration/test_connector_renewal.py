"""Daemon-managed file connector renewal against disposable encrypted vaults."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tessera.auth import tokens
from tessera.auth.scopes import Scope, build_scope
from tessera.connectors import get_connector
from tessera.connectors.base import McpServerSpec
from tessera.connectors.renewal import (
    adopt_default_installations,
    due,
    reconcile_pending,
    register,
    renew_due,
)
from tessera.observability.events import EventLog
from tessera.vault.connection import VaultConnection

_NOW = 1_000_000
_TTL = 90 * 24 * 60 * 60
_HORIZON = 14 * 24 * 60 * 60
_FILE_CONNECTORS = (
    "claude-desktop",
    "claude-code",
    "cursor",
    "codex",
    "opencode",
    "omp",
    "pi",
)


def _new_agent(conn: VaultConnection) -> int:
    cur = conn.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES (?, ?, ?)",
        ("01RENEWAL", "renewal-test", _NOW),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _issue(
    conn: VaultConnection,
    *,
    agent_id: int,
    client_name: str,
    ttl_seconds: int = _TTL,
    scope: Scope | None = None,
) -> tokens.IssuedToken:
    return tokens.issue(
        conn.connection,
        agent_id=agent_id,
        client_name=client_name,
        token_class="service",
        scope=scope or build_scope(read=["style", "project"], write=["style"]),
        now_epoch=_NOW,
        access_ttl_seconds=ttl_seconds,
    )


def _config_path(tmp_path: Path, connector_id: str) -> Path:
    suffix = ".toml" if connector_id == "codex" else ".json"
    return tmp_path / f"{connector_id}{suffix}"


def _write_installation(
    conn: VaultConnection,
    *,
    tmp_path: Path,
    agent_id: int,
    connector_id: str,
    ttl_seconds: int = _TTL,
    scope: Scope | None = None,
) -> tuple[Path, tokens.IssuedToken]:
    issued = _issue(
        conn,
        agent_id=agent_id,
        client_name=connector_id,
        ttl_seconds=ttl_seconds,
        scope=scope,
    )
    path = _config_path(tmp_path, connector_id)
    connector = get_connector(connector_id)
    connector.apply(path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=issued.raw_token))
    register(
        conn.connection,
        connector_id=connector_id,
        config_path=str(path.resolve()),
        agent_id=agent_id,
        capability_id=issued.token_id,
        access_ttl_seconds=_TTL,
        now_epoch=_NOW,
    )
    return path, issued


def _installation_row(conn: VaultConnection) -> tuple[int, int, int | None]:
    row = conn.connection.execute(
        "SELECT id, active_capability_id, pending_capability_id "
        "FROM managed_connector_installations"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2]) if row[2] is not None else None


def _audit_payloads(conn: VaultConnection, *, prefix: str) -> list[dict[str, object]]:
    rows = conn.connection.execute(
        "SELECT payload FROM audit_log WHERE op LIKE ? ORDER BY id", (f"{prefix}%",)
    ).fetchall()
    return [json.loads(str(row[0])) for row in rows]


@pytest.mark.integration
def test_registers_custom_path_only_after_successful_write(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    path = _config_path(tmp_path, "cursor")
    issued = _issue(open_vault, agent_id=agent_id, client_name="cursor")

    assert (
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM managed_connector_installations"
        ).fetchone()[0]
        == 0
    )

    get_connector("cursor").apply(
        path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=issued.raw_token)
    )
    register(
        open_vault.connection,
        connector_id="cursor",
        config_path=str(path.resolve()),
        agent_id=agent_id,
        capability_id=issued.token_id,
        access_ttl_seconds=_TTL,
        now_epoch=_NOW,
    )

    row = open_vault.connection.execute(
        "SELECT connector_id, config_path, agent_id, active_capability_id, pending_capability_id, "
        "access_ttl_seconds FROM managed_connector_installations"
    ).fetchone()
    assert tuple(row) == ("cursor", str(path.resolve()), agent_id, issued.token_id, None, _TTL)
    payload = _audit_payloads(open_vault, prefix="connector_installation_")[-1]
    assert payload == {
        "access_ttl_seconds": _TTL,
        "active_capability_id": issued.token_id,
        "config_path": str(path.resolve()),
        "connector_id": "cursor",
        "installation_id": 1,
    }


@pytest.mark.integration
def test_adopts_only_a_valid_existing_default_path(
    open_vault: VaultConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_id = _new_agent(open_vault)
    connector = get_connector("claude-code")
    issued = _issue(open_vault, agent_id=agent_id, client_name="claude-code")
    default_path = connector.default_path()
    connector.apply(
        default_path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=issued.raw_token)
    )

    adopted = adopt_default_installations(open_vault.connection, now_epoch=_NOW)

    assert adopted == 1
    row = open_vault.connection.execute(
        "SELECT connector_id, config_path, agent_id, active_capability_id "
        "FROM managed_connector_installations"
    ).fetchone()
    assert tuple(row) == ("claude-code", str(default_path.resolve()), agent_id, issued.token_id)


@pytest.mark.integration
def test_adoption_skips_custom_expired_malformed_and_mismatched_configs(
    open_vault: VaultConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_id = _new_agent(open_vault)
    custom_path = tmp_path / "custom.json"
    custom_issued = _issue(open_vault, agent_id=agent_id, client_name="cursor")
    get_connector("cursor").apply(
        custom_path,
        McpServerSpec(url="http://127.0.0.1:5710/mcp", token=custom_issued.raw_token),
    )
    expired = _issue(
        open_vault,
        agent_id=agent_id,
        client_name="claude-code",
        ttl_seconds=1,
    )
    claude_code_path = get_connector("claude-code").default_path()
    get_connector("claude-code").apply(
        claude_code_path,
        McpServerSpec(url="http://127.0.0.1:5710/mcp", token=expired.raw_token),
    )
    get_connector("cursor").default_path().parent.mkdir(parents=True, exist_ok=True)
    get_connector("cursor").default_path().write_text("{not json", encoding="utf-8")
    mismatched = _issue(open_vault, agent_id=agent_id, client_name="cursor")
    get_connector("omp").default_path().parent.mkdir(parents=True, exist_ok=True)
    get_connector("omp").apply(
        get_connector("omp").default_path(),
        McpServerSpec(url="http://127.0.0.1:5710/mcp", token=mismatched.raw_token),
    )

    assert adopt_default_installations(open_vault.connection, now_epoch=_NOW + 2) == 0
    assert (
        open_vault.connection.execute(
            "SELECT COUNT(*) FROM managed_connector_installations"
        ).fetchone()[0]
        == 0
    )
    assert custom_path.exists()


@pytest.mark.integration
@pytest.mark.parametrize("connector_id", _FILE_CONNECTORS)
def test_due_renewal_preserves_identity_scope_class_and_old_token(
    open_vault: VaultConnection, tmp_path: Path, connector_id: str
) -> None:
    agent_id = _new_agent(open_vault)
    scope = build_scope(read=["style", "project"], write=["style"])
    path, old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id=connector_id,
        ttl_seconds=_HORIZON - 1,
        scope=scope,
    )
    events = EventLog.open(tmp_path / "events.db")
    try:
        assert (
            renew_due(
                open_vault.connection,
                now_epoch=_NOW,
                horizon_seconds=_HORIZON,
                url="http://127.0.0.1:5710/mcp",
                event_log=events,
            )
            == 1
        )
    finally:
        events.close()

    new_raw = get_connector(connector_id).read_token(path)
    assert new_raw is not None
    assert new_raw != old.raw_token
    old_verified = tokens.resolve_live_without_touch(
        open_vault.connection, raw_token=old.raw_token, now_epoch=_NOW + 1
    )
    new_verified = tokens.resolve_live_without_touch(
        open_vault.connection, raw_token=new_raw, now_epoch=_NOW + 1
    )
    assert old_verified is not None
    assert new_verified is not None
    assert new_verified.agent_id == agent_id
    assert new_verified.client_name == connector_id
    assert new_verified.token_class == "service"
    assert new_verified.scope == scope
    assert new_verified.expires_at == _NOW + _TTL
    _, active_id, pending_id = _installation_row(open_vault)
    assert active_id == new_verified.token_id
    assert pending_id is None


@pytest.mark.integration
def test_outside_horizon_does_not_rewrite_or_mint(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    path, _old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="codex",
        ttl_seconds=_HORIZON + 1,
    )
    before = path.read_bytes()
    capability_count = open_vault.connection.execute(
        "SELECT COUNT(*) FROM capabilities"
    ).fetchone()[0]

    assert (
        renew_due(
            open_vault.connection,
            now_epoch=_NOW,
            horizon_seconds=_HORIZON,
            url="http://127.0.0.1:5710/mcp",
        )
        == 0
    )

    assert path.read_bytes() == before
    assert (
        open_vault.connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]
        == capability_count
    )
    assert due(open_vault.connection, now_epoch=_NOW, horizon_seconds=_HORIZON) == []


@pytest.mark.integration
def test_config_write_failure_revokes_staged_replacement_and_preserves_old_config(
    open_vault: VaultConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _new_agent(open_vault)
    path, old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="claude-code",
        ttl_seconds=_HORIZON - 1,
    )
    before = path.read_bytes()
    connector_type = type(get_connector("claude-code"))

    def _raise_write_error(self: object, path: Path, server: McpServerSpec) -> object:
        del self, path, server
        raise OSError("forced config write failure")

    monkeypatch.setattr(connector_type, "apply", _raise_write_error)
    assert (
        renew_due(
            open_vault.connection,
            now_epoch=_NOW,
            horizon_seconds=_HORIZON,
            url="http://127.0.0.1:5710/mcp",
        )
        == 0
    )

    assert path.read_bytes() == before
    _, active_id, pending_id = _installation_row(open_vault)
    assert active_id == old.token_id
    assert pending_id is None
    revoked = open_vault.connection.execute(
        "SELECT revoked_at FROM capabilities WHERE id != ? ORDER BY id DESC LIMIT 1",
        (old.token_id,),
    ).fetchone()
    assert revoked is not None
    assert revoked[0] == _NOW
    assert (
        tokens.resolve_live_without_touch(
            open_vault.connection, raw_token=old.raw_token, now_epoch=_NOW + 1
        )
        is not None
    )
    payload = _audit_payloads(open_vault, prefix="connector_renewal_write_failed")[-1]
    assert payload["failure_class"] == "OSError"


@pytest.mark.integration
def test_restart_reconciliation_promotes_pending_file_token(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    path, old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="omp",
        ttl_seconds=_HORIZON - 1,
    )
    pending = _issue(open_vault, agent_id=agent_id, client_name="omp")
    installation_id, _, _ = _installation_row(open_vault)
    open_vault.connection.execute(
        "UPDATE managed_connector_installations SET pending_capability_id = ? WHERE id = ?",
        (pending.token_id, installation_id),
    )
    get_connector("omp").apply(
        path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=pending.raw_token)
    )

    assert reconcile_pending(open_vault.connection, now_epoch=_NOW) == 1

    _, active_id, pending_id = _installation_row(open_vault)
    assert active_id == pending.token_id
    assert pending_id is None
    assert (
        tokens.resolve_live_without_touch(
            open_vault.connection, raw_token=old.raw_token, now_epoch=_NOW + 1
        )
        is not None
    )


@pytest.mark.integration
def test_restart_reconciliation_discards_unwritten_pending_token(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    _path, old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="pi",
        ttl_seconds=_HORIZON - 1,
    )
    pending = _issue(open_vault, agent_id=agent_id, client_name="pi")
    installation_id, _, _ = _installation_row(open_vault)
    open_vault.connection.execute(
        "UPDATE managed_connector_installations SET pending_capability_id = ? WHERE id = ?",
        (pending.token_id, installation_id),
    )

    assert reconcile_pending(open_vault.connection, now_epoch=_NOW) == 1

    _, active_id, pending_id = _installation_row(open_vault)
    assert active_id == old.token_id
    assert pending_id is None
    assert (
        tokens.resolve_live_without_touch(
            open_vault.connection, raw_token=pending.raw_token, now_epoch=_NOW + 1
        )
        is None
    )


@pytest.mark.integration
def test_restart_reconciliation_records_drift_without_mutating_pending(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    path, _old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="opencode",
        ttl_seconds=_HORIZON - 1,
    )
    pending = _issue(open_vault, agent_id=agent_id, client_name="opencode")
    drifted = _issue(open_vault, agent_id=agent_id, client_name="opencode")
    installation_id, active_id, _ = _installation_row(open_vault)
    open_vault.connection.execute(
        "UPDATE managed_connector_installations SET pending_capability_id = ? WHERE id = ?",
        (pending.token_id, installation_id),
    )
    get_connector("opencode").apply(
        path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=drifted.raw_token)
    )

    assert reconcile_pending(open_vault.connection, now_epoch=_NOW) == 0

    _, observed_active, observed_pending = _installation_row(open_vault)
    assert observed_active == active_id
    assert observed_pending == pending.token_id
    assert (
        tokens.resolve_live_without_touch(
            open_vault.connection, raw_token=pending.raw_token, now_epoch=_NOW + 1
        )
        is not None
    )
    assert _audit_payloads(open_vault, prefix="connector_config_drift")[-1]["state"] == "neither"


@pytest.mark.integration
def test_renewal_audit_and_events_exclude_secrets(
    open_vault: VaultConnection, tmp_path: Path
) -> None:
    agent_id = _new_agent(open_vault)
    path, old = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="claude-desktop",
        ttl_seconds=_HORIZON - 1,
    )
    events = EventLog.open(tmp_path / "events.db")
    try:
        assert (
            renew_due(
                open_vault.connection,
                now_epoch=_NOW,
                horizon_seconds=_HORIZON,
                url="http://127.0.0.1:5710/mcp",
                event_log=events,
            )
            == 1
        )
        new_raw = get_connector("claude-desktop").read_token(path)
        assert new_raw is not None
        rendered = "\n".join(
            json.dumps(payload, sort_keys=True)
            for payload in _audit_payloads(open_vault, prefix="connector_")
        )
        event_rendered = "\n".join(
            json.dumps(event.attrs, sort_keys=True)
            for event in events.recent(limit=20, min_level="debug")
        )
    finally:
        events.close()

    assert old.raw_token not in rendered
    assert new_raw not in rendered
    assert old.raw_token not in event_rendered
    assert new_raw not in event_rendered


@pytest.mark.integration
def test_disposable_vault_renewal_lifecycle_end_to_end(
    open_vault: VaultConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise renewal, old-token continuity, crash recovery, and write rollback."""

    agent_id = _new_agent(open_vault)
    path, original = _write_installation(
        open_vault,
        tmp_path=tmp_path,
        agent_id=agent_id,
        connector_id="claude-code",
        ttl_seconds=_HORIZON - 1,
    )
    events = EventLog.open(tmp_path / "events.db")
    try:
        assert (
            renew_due(
                open_vault.connection,
                now_epoch=_NOW,
                horizon_seconds=_HORIZON,
                url="http://127.0.0.1:5710/mcp",
                event_log=events,
            )
            == 1
        )
        first_replacement = get_connector("claude-code").read_token(path)
        assert first_replacement is not None
        assert (
            tokens.resolve_live_without_touch(
                open_vault.connection, raw_token=original.raw_token, now_epoch=_NOW + 1
            )
            is not None
        )

        staged = _issue(open_vault, agent_id=agent_id, client_name="claude-code")
        installation_id, _, _ = _installation_row(open_vault)
        open_vault.connection.execute(
            "UPDATE managed_connector_installations SET pending_capability_id = ? WHERE id = ?",
            (staged.token_id, installation_id),
        )
        get_connector("claude-code").apply(
            path, McpServerSpec(url="http://127.0.0.1:5710/mcp", token=staged.raw_token)
        )
        assert reconcile_pending(open_vault.connection, now_epoch=_NOW, event_log=events) == 1
        _, active_id, pending_id = _installation_row(open_vault)
        assert active_id == staged.token_id
        assert pending_id is None

        open_vault.connection.execute(
            "UPDATE capabilities SET expires_at = ? WHERE id = ?",
            (_NOW + _HORIZON - 1, staged.token_id),
        )
        before_failure = path.read_bytes()

        def _raise_write_error(self: object, config_path: Path, server: McpServerSpec) -> object:
            del self, config_path, server
            raise OSError("forced config write failure")

        monkeypatch.setattr(type(get_connector("claude-code")), "apply", _raise_write_error)
        assert (
            renew_due(
                open_vault.connection,
                now_epoch=_NOW,
                horizon_seconds=_HORIZON,
                url="http://127.0.0.1:5710/mcp",
                event_log=events,
            )
            == 0
        )
    finally:
        events.close()

    _, active_id, pending_id = _installation_row(open_vault)
    assert active_id == staged.token_id
    assert pending_id is None
    assert path.read_bytes() == before_failure
