"""Tests for the `tessera vault repair-embeds` stub CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli import vault as cli_vault
from tessera.vault import capture
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, save_salt


def _make_agent(vc: VaultConnection) -> int:
    cur = vc.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01REPAIR', 'agent', 0)"
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _seed_failed(vc: VaultConnection, agent_id: int, *, facet_type: str, n: int) -> None:
    for i in range(n):
        capture.capture(
            vc.connection,
            agent_id=agent_id,
            facet_type=facet_type,
            content=f"{facet_type}-{i}",
            source_tool="test",
        )
    vc.connection.execute(
        "UPDATE facets SET embed_status='failed', embed_attempts=4, "
        "embed_last_error='x', embed_last_attempt_at=100 "
        "WHERE facet_type=?",
        (facet_type,),
    )


@pytest.mark.unit
def test_repair_embeds_resets_all_failed(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _seed_failed(open_vault, agent_id, facet_type="project", n=2)
    _seed_failed(open_vault, agent_id, facet_type="preference", n=3)

    updated = cli_vault.repair_embeds(open_vault.connection)

    assert updated == 5
    row = open_vault.connection.execute(
        "SELECT COUNT(*) FROM facets WHERE embed_status='pending' AND embed_attempts=0"
    ).fetchone()
    assert int(row[0]) == 5


@pytest.mark.unit
def test_repair_embeds_filters_by_facet_type(open_vault: VaultConnection) -> None:
    agent_id = _make_agent(open_vault)
    _seed_failed(open_vault, agent_id, facet_type="project", n=2)
    _seed_failed(open_vault, agent_id, facet_type="preference", n=3)

    updated = cli_vault.repair_embeds(open_vault.connection, facet_type="project")

    assert updated == 2
    project_row = open_vault.connection.execute(
        "SELECT COUNT(*) FROM facets WHERE facet_type='project' AND embed_status='pending'"
    ).fetchone()
    preference_row = open_vault.connection.execute(
        "SELECT COUNT(*) FROM facets WHERE facet_type='preference' AND embed_status='failed'"
    ).fetchone()
    assert int(project_row[0]) == 2
    assert int(preference_row[0]) == 3


@pytest.mark.unit
def test_repair_embeds_rejects_unsupported_facet_type(open_vault: VaultConnection) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        cli_vault.repair_embeds(open_vault.connection, facet_type="compiled_notebook")


@pytest.mark.unit
def test_cli_entrypoint_runs_end_to_end(
    vault_path: Path,
    vault_key: ProtectedKey,
    open_vault: VaultConnection,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _make_agent(open_vault)
    _seed_failed(open_vault, agent_id, facet_type="project", n=1)
    open_vault.close()

    key_bytes = bytes.fromhex(vault_key.hex())

    def fake_derive(*_a: object, **_k: object) -> ProtectedKey:
        return ProtectedKey.adopt(key_bytes)

    save_salt(vault_path, b"\x00" * 16)
    monkeypatch.setattr(cli_vault, "derive_key", fake_derive)

    rc = cli_vault.run(["repair-embeds", "--vault", str(vault_path), "--passphrase", "pw"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reset 1" in out


@pytest.mark.unit
def test_cli_missing_salt_sidecar_returns_nonzero(
    vault_path: Path,
    open_vault: VaultConnection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    open_vault.close()
    # No save_salt call — the CLI must fail with a clear diagnostic.
    rc = cli_vault.run(["repair-embeds", "--vault", str(vault_path), "--passphrase", "pw"])
    assert rc == 1
    assert "no KDF salt sidecar" in capsys.readouterr().err
