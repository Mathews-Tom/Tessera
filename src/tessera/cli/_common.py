"""Shared helpers for CLI subcommands.

Passphrase resolution, vault unlock, and output formatting live here
so every subcommand has one path for the same primitives.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

from tessera.cli._ui import error as _ui_error
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey, derive_key, load_salt


class CliError(Exception):
    """User-facing error rendered to stderr with a nonzero exit code."""


def resolve_passphrase(arg_value: str | None) -> bytearray:
    """Pick the passphrase from --passphrase, env, or error out.

    Returns a bytearray so the caller can wipe it via ``derive_key``'s
    context manager. The env-var name comes from
    ``TESSERA_PASSPHRASE_ENV`` when set, else ``TESSERA_PASSPHRASE`` —
    mirroring the unit-file generation in daemon/units.py.
    """

    if arg_value:
        return bytearray(arg_value.encode("utf-8"))
    env_var = os.environ.get("TESSERA_PASSPHRASE_ENV", "TESSERA_PASSPHRASE")
    env_value = os.environ.get(env_var)
    if env_value:
        return bytearray(env_value.encode("utf-8"))
    raise CliError(f"passphrase required; pass --passphrase or export {env_var}")


@contextlib.contextmanager
def open_vault(vault_path: Path, passphrase: bytearray) -> Iterator[VaultConnection]:
    """Unlock the vault at ``vault_path`` with ``passphrase``.

    Raises :class:`CliError` when the sidecar salt is missing (vault
    not initialised). The key is wiped on context exit via
    :class:`ProtectedKey`'s context management.
    """

    try:
        salt = load_salt(vault_path)
    except FileNotFoundError as exc:
        raise CliError(f"no KDF salt sidecar for {vault_path}; run `tessera init` first") from exc
    key: ProtectedKey = derive_key(passphrase, salt)
    with key, VaultConnection.open(vault_path, key) as vc:
        yield vc


def fail(message: str) -> int:
    """Emit a red ✗ ERROR line to stderr and return exit code 1.

    The literal ``ERROR`` token is preserved so ``grep`` scripts keep
    working; the ✗ and colour layer on top for TTY readability.
    """

    _ui_error(message)
    return 1


__all__ = [
    "CliError",
    "fail",
    "open_vault",
    "resolve_passphrase",
]
