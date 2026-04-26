"""Doctor checks that require an unlocked vault.

Complements ``tests/unit/test_daemon_doctor.py`` which covers the
vault-less path: here we open a freshly bootstrapped vault, register a
model, issue a token, and assert every vault-backed check reports OK.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tessera.adapters import models_registry
from tessera.auth import tokens
from tessera.auth.scopes import build_scope
from tessera.daemon.config import resolve_config
from tessera.daemon.doctor import DoctorStatus, run_all
from tessera.vault.connection import VaultConnection


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_vault_checks_ok_on_healthy_vault(
    open_vault: VaultConnection,
    vault_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TESSERA_PASSPHRASE", "x")
    # Force a fastembed cache directory we know exists so the
    # _check_fastembed_cache check returns OK.
    cache_dir = tmp_path / "fastembed-cache"
    cache_dir.mkdir()
    monkeypatch.setenv("FASTEMBED_CACHE_DIR", str(cache_dir))
    os.environ["FASTEMBED_CACHE_DIR"] = str(cache_dir)
    models_registry.register_embedding_model(
        open_vault.connection, name="nomic-ai/nomic-embed-text-v1.5", dim=8, activate=True
    )
    open_vault.connection.execute(
        "INSERT INTO agents(external_id, name, created_at) VALUES ('01DR', 'a', 0)"
    )
    tokens.issue(
        open_vault.connection,
        agent_id=1,
        client_name="cli",
        token_class="session",
        scope=build_scope(read=["style"], write=[]),
        now_epoch=1_000_000,
    )

    config = resolve_config(vault_path=vault_path, http_port=_pick_port())
    report = await run_all(config, conn=open_vault.connection)
    names = {r.name for r in report.results}
    assert {"sqlite_vec", "active_model", "schema_match", "tokens"} <= names
    by_name = {r.name: r for r in report.results}
    assert by_name["sqlite_vec"].status is DoctorStatus.OK
    assert by_name["active_model"].status is DoctorStatus.OK
    assert by_name["schema_match"].status is DoctorStatus.OK
    assert by_name["tokens"].status is DoctorStatus.OK
    assert by_name["fastembed"].status is DoctorStatus.OK


@pytest.mark.integration
@pytest.mark.asyncio
async def test_doctor_reports_missing_active_model(
    open_vault: VaultConnection, vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No active embedding model → active_model check is ERROR."""

    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    config = resolve_config(vault_path=vault_path, http_port=_pick_port())
    report = await run_all(config, conn=open_vault.connection)
    active = next(r for r in report.results if r.name == "active_model")
    assert active.status is DoctorStatus.ERROR


@pytest.mark.integration
@pytest.mark.asyncio
async def test_doctor_reports_no_tokens_warning(
    open_vault: VaultConnection, vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly bootstrapped vault has no capabilities → WARN."""

    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    config = resolve_config(vault_path=vault_path, http_port=_pick_port())
    report = await run_all(config, conn=open_vault.connection)
    tokens_check = next(r for r in report.results if r.name == "tokens")
    assert tokens_check.status is DoctorStatus.WARN


@pytest.mark.integration
@pytest.mark.asyncio
async def test_doctor_reports_empty_facet_types_warning(
    open_vault: VaultConnection, vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty vault → facet_types check is WARN listing every v0.1 type.

    Closes DoD item 3 in docs/release-spec.md §v0.1 DoD: doctor must
    diagnose "empty facet types".
    """

    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    config = resolve_config(vault_path=vault_path, http_port=_pick_port())
    report = await run_all(config, conn=open_vault.connection)
    facet_check = next(r for r in report.results if r.name == "facet_types")
    assert facet_check.status is DoctorStatus.WARN
    for facet_type in ("identity", "preference", "workflow", "project", "style"):
        assert facet_type in facet_check.detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_doctor_facet_types_ok_when_all_populated(
    open_vault: VaultConnection, vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault with at least one row per v0.1 facet type → OK."""

    from tessera.vault import capture as vault_capture

    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    conn = open_vault.connection
    conn.execute("INSERT INTO agents(external_id, name, created_at) VALUES ('01F', 'a', 0)")
    agent_id = int(conn.execute("SELECT id FROM agents WHERE external_id='01F'").fetchone()[0])
    for ft in ("identity", "preference", "workflow", "project", "style"):
        vault_capture.capture(
            conn,
            agent_id=agent_id,
            facet_type=ft,
            content=f"{ft} content",
            source_tool="test",
            captured_at=1_000_000,
        )

    config = resolve_config(vault_path=vault_path, http_port=_pick_port())
    report = await run_all(config, conn=conn)
    facet_check = next(r for r in report.results if r.name == "facet_types")
    assert facet_check.status is DoctorStatus.OK


def _pick_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port
