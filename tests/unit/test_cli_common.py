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
    """Multiple *.db files with paired .salt sidecars → CliError.

    The disambiguation guard counts only files structured as real
    vaults (paired ``.db.salt`` sidecar). Two such files with no
    explicit choice triggers the refuse-to-guess error.
    """

    monkeypatch.delenv("TESSERA_VAULT", raising=False)
    (tmp_path / "work.db").write_bytes(b"")
    (tmp_path / "work.db.salt").write_bytes(b"\x00" * 16)
    (tmp_path / "personal.db").write_bytes(b"")
    (tmp_path / "personal.db.salt").write_bytes(b"\x00" * 16)
    monkeypatch.setattr("tessera.cli._common.DEFAULT_VAULT_DIR", tmp_path)
    with pytest.raises(CliError) as exc_info:
        resolve_vault_path(None)
    msg = str(exc_info.value)
    assert "multiple vaults" in msg
    assert "work.db" in msg
    assert "personal.db" in msg


@pytest.mark.unit
def test_resolve_vault_path_ignores_db_files_without_salt_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon-internal SQLite files (no .salt sidecar) do not trigger the guard.

    Closes the disambiguation-warning issue where ``events.db`` at
    the top level of ``~/.tessera/`` collided with the real
    ``vault.db`` and made every CLI command refuse to guess. The
    fix relocated ``events.db`` under ``~/.tessera/run/``; this test
    pins the defence-in-depth filter on the resolver itself so a
    future auxiliary ``.db`` at the top level cannot reintroduce the
    collision.
    """

    monkeypatch.delenv("TESSERA_VAULT", raising=False)
    (tmp_path / "vault.db").write_bytes(b"")
    (tmp_path / "vault.db.salt").write_bytes(b"\x00" * 16)
    # An auxiliary plain-SQLite database (the shape pre-v0.1.x
    # ``events.db`` used to take). No ``.salt`` sidecar — must not
    # count as a vault.
    (tmp_path / "events.db").write_bytes(b"")
    monkeypatch.setattr("tessera.cli._common.DEFAULT_VAULT_DIR", tmp_path)
    monkeypatch.setattr("tessera.cli._common.DEFAULT_VAULT_PATH", tmp_path / "vault.db")
    # No CliError — single salted candidate resolves cleanly.
    assert resolve_vault_path(None) == tmp_path / "vault.db"


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
