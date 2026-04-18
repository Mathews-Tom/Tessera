"""Keyring caching API against an in-memory keyring backend."""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend

from tessera.vault import keyring_cache


class _MemoryKeyring(KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            del self._store[(service, username)]
        except KeyError as exc:
            raise PasswordDeleteError(str(exc)) from exc


@pytest.fixture
def mem_keyring() -> Iterator[_MemoryKeyring]:
    backend = _MemoryKeyring()
    prior = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(prior)


@pytest.mark.unit
@pytest.mark.usefixtures("mem_keyring")
def test_cache_and_load_round_trip() -> None:
    keyring_cache.cache_passphrase("01VAULT", b"some phrase")
    loaded = keyring_cache.load_passphrase("01VAULT")
    assert loaded == bytearray(b"some phrase")


@pytest.mark.unit
@pytest.mark.usefixtures("mem_keyring")
def test_load_missing_returns_none() -> None:
    assert keyring_cache.load_passphrase("01ABSENT") is None


@pytest.mark.unit
@pytest.mark.usefixtures("mem_keyring")
def test_clear_existing_returns_true() -> None:
    keyring_cache.cache_passphrase("01V", b"secret")
    assert keyring_cache.clear_passphrase("01V") is True
    assert keyring_cache.load_passphrase("01V") is None


@pytest.mark.unit
@pytest.mark.usefixtures("mem_keyring")
def test_clear_missing_returns_false() -> None:
    assert keyring_cache.clear_passphrase("01MISSING") is False


@pytest.mark.unit
def test_empty_vault_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        keyring_cache.cache_passphrase("", b"x")
    with pytest.raises(ValueError, match="non-empty"):
        keyring_cache.load_passphrase("")
    with pytest.raises(ValueError, match="non-empty"):
        keyring_cache.clear_passphrase("")


@pytest.mark.unit
@pytest.mark.usefixtures("mem_keyring")
def test_passphrase_with_non_ascii_bytes_round_trips() -> None:
    raw = bytes(range(256))
    keyring_cache.cache_passphrase("01V", raw)
    assert bytes(keyring_cache.load_passphrase("01V") or b"") == raw
