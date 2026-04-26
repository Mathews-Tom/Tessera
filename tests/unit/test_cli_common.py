"""Resolution helpers in ``tessera.cli._common``: vault path + passphrase."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.cli._common import (
    DEFAULT_VAULT_PATH,
    CliError,
    resolve_passphrase,
    resolve_vault_path,
)


@pytest.mark.unit
def test_resolve_vault_path_prefers_explicit_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERA_VAULT", str(tmp_path / "ignored.db"))
    explicit = tmp_path / "explicit.db"
    assert resolve_vault_path(explicit) == explicit


@pytest.mark.unit
def test_resolve_vault_path_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("TESSERA_VAULT", str(target))
    assert resolve_vault_path(None) == target


@pytest.mark.unit
def test_resolve_vault_path_expands_user_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESSERA_VAULT", "~/custom/vault.db")
    resolved = resolve_vault_path(None)
    assert "~" not in str(resolved)
    assert resolved.name == "vault.db"


@pytest.mark.unit
def test_resolve_vault_path_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERA_VAULT", raising=False)
    monkeypatch.setattr(
        "tessera.cli._common.DEFAULT_VAULT_DIR",
        Path("/nonexistent-tessera-test-dir-1234"),
    )
    assert resolve_vault_path(None) == DEFAULT_VAULT_PATH


@pytest.mark.unit
def test_resolve_vault_path_disambiguates_multi_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple *.db files in the default dir with no explicit choice → CliError."""

    monkeypatch.delenv("TESSERA_VAULT", raising=False)
    (tmp_path / "work.db").write_bytes(b"")
    (tmp_path / "personal.db").write_bytes(b"")
    monkeypatch.setattr("tessera.cli._common.DEFAULT_VAULT_DIR", tmp_path)
    with pytest.raises(CliError) as exc_info:
        resolve_vault_path(None)
    msg = str(exc_info.value)
    assert "multiple vaults" in msg
    assert "work.db" in msg
    assert "personal.db" in msg


@pytest.mark.unit
def test_resolve_passphrase_arg_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "from-env")
    assert bytes(resolve_passphrase("from-arg")) == b"from-arg"


@pytest.mark.unit
def test_resolve_passphrase_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERA_PASSPHRASE", "env-only")
    monkeypatch.delenv("TESSERA_PASSPHRASE_ENV", raising=False)
    assert bytes(resolve_passphrase(None)) == b"env-only"


@pytest.mark.unit
def test_resolve_passphrase_error_points_at_persistent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TESSERA_PASSPHRASE", raising=False)
    monkeypatch.delenv("TESSERA_PASSPHRASE_ENV", raising=False)
    with pytest.raises(CliError) as exc_info:
        resolve_passphrase(None)
    msg = str(exc_info.value)
    assert "export TESSERA_PASSPHRASE" in msg
    assert "--passphrase" in msg
