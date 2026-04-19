"""Smoke tests for the ``tessera models`` stub CLI.

The full CLI is P9; this exercises only the stub surface required by the P2
exit gate ("tessera models list/set/test stub works against Ollama adapter").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tessera.adapters import models_registry
from tessera.cli import models as cli_models
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, save_salt


@pytest.mark.unit
def test_list_prints_known_adapters(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_models.run(["list"]) == 0
    out = capsys.readouterr().out
    assert "ollama" in out
    assert "sentence-transformers" in out


@pytest.mark.unit
def test_set_registers_model(
    vault_path: Path,
    vault_key: ProtectedKey,
    open_vault: VaultConnection,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The set subcommand wipes its derived key when its ``with`` block exits,
    # so the fake_derive hands out a fresh ProtectedKey with the same bytes
    # as the fixture's key. The fixture's key stays live for the re-open
    # assertion below.
    key_bytes = bytes.fromhex(vault_key.hex())

    def fake_derive(*_a: object, **_k: object) -> ProtectedKey:
        return ProtectedKey.adopt(key_bytes)

    # Write a sidecar so the CLI's salt-load path is exercised for real.
    # derive_key is still stubbed because fixture-generated keys bypass the
    # real argon2id cost.
    save_salt(vault_path, b"\x00" * 16)
    monkeypatch.setattr(cli_models, "derive_key", fake_derive)
    open_vault.close()

    rc = cli_models.run(
        [
            "set",
            "--vault",
            str(vault_path),
            "--passphrase",
            "pw",
            "--name",
            "ollama",
            "--model",
            "nomic-embed-text",
            "--dim",
            "768",
            "--activate",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "registered" in out

    with VaultConnection.open(vault_path, vault_key) as vc:
        models = models_registry.list_models(vc.connection)
        assert [m.name for m in models] == ["ollama"]
        assert models[0].is_active is True


@pytest.mark.unit
def test_test_reports_failure_when_ollama_unreachable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_unhealthy(self: Any) -> None:
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(
        "tessera.adapters.ollama_embedder.OllamaEmbedder.health_check",
        _raise_unhealthy,
    )
    rc = cli_models.run(["test", "--model", "nomic-embed-text"])
    assert rc == 1
    assert "health_check failed" in capsys.readouterr().err


@pytest.mark.unit
def test_test_reports_success(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _ok(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "tessera.adapters.ollama_embedder.OllamaEmbedder.health_check",
        _ok,
    )
    rc = cli_models.run(["test", "--model", "nomic-embed-text"])
    assert rc == 0
    assert "reachable" in capsys.readouterr().out
