"""Shared pytest fixtures for the Tessera test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt

_DEFAULT_PASSPHRASE = b"correct horse battery staple"


@pytest.fixture
def passphrase() -> bytearray:
    return bytearray(_DEFAULT_PASSPHRASE)


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


@pytest.fixture
def vault_key(passphrase: bytearray) -> Iterator[ProtectedKey]:
    salt = new_salt()
    key = derive_key(passphrase, salt)
    yield key
    key.wipe()


@pytest.fixture
def open_vault(vault_path: Path, vault_key: ProtectedKey) -> Iterator[VaultConnection]:
    bootstrap(vault_path, vault_key)
    # derive a fresh key for the open because bootstrap kept key alive.
    with VaultConnection.open(vault_path, vault_key) as vc:
        yield vc
