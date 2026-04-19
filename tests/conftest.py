"""Shared pytest fixtures for the Tessera test suite."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tessera.migration import bootstrap
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, new_salt

_DEFAULT_PASSPHRASE = b"correct horse battery staple"

# No-outbound gate (CI enforcement per docs/determinism-and-observability.md).
#
# When ``TESSERA_NO_OUTBOUND=1`` is set, every ``socket.socket.connect`` call
# is wrapped; non-loopback destinations raise ``OSError``. Any transitive
# dependency that makes a hidden outbound call (telemetry, update check,
# auto-download) surfaces as a test failure instead of silently succeeding.
#
# Enforcement lives at the Python socket layer rather than at iptables
# because GitHub-hosted runners keep a persistent outbound connection to
# the Actions control plane — a kernel-level REJECT on all non-loopback
# traffic strands the runner agent and leaves the job hung until timeout.
# Socket-layer enforcement isolates the test process instead. The tradeoff
# is that a dependency using raw ctypes syscalls would bypass the guard;
# none in the current dependency graph do.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


def _install_no_outbound_guard() -> None:
    original_connect = socket.socket.connect

    def _guarded(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and len(address) >= 1:
            host = address[0]
            if isinstance(host, str) and (host in _LOOPBACK_HOSTS or host.startswith("127.")):
                original_connect(self, address)
                return
            raise OSError(
                f"no-outbound gate: socket.connect to {host!r} blocked "
                "(only loopback allowed under TESSERA_NO_OUTBOUND=1)"
            )
        if isinstance(address, str | bytes):
            # Unix-domain socket: file-backed, no network egress.
            original_connect(self, address)
            return
        raise OSError(f"no-outbound gate: unexpected address shape {address!r}")

    socket.socket.connect = _guarded  # type: ignore[assignment,method-assign]


if os.environ.get("TESSERA_NO_OUTBOUND") == "1":
    _install_no_outbound_guard()


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
    # bootstrap() does not wipe the key on return, so the same ProtectedKey
    # is still live here. If bootstrap ever starts wiping on exit, this
    # fixture becomes the first failing test and points at the change.
    with VaultConnection.open(vault_path, vault_key) as vc:
        yield vc
