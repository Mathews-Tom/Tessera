"""Shared helpers for CLI subcommands.

Passphrase resolution, vault unlock, and output formatting live here
so every subcommand has one path for the same primitives.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

import sqlcipher3

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


def resolve_agent_id(conn: sqlcipher3.Connection, explicit: int | None) -> int:
    """Pick the target agent id for per-agent CLI operations.

    When ``explicit`` is provided (``--agent-id N`` on the CLI), trust
    it — operations against a non-existent agent_id hit foreign-key
    errors at the vault layer, which surface as loud CLI failures
    anyway.

    When ``explicit`` is None, auto-select the single agent in the
    vault. This is the common case after ``tessera init`` creates its
    one default agent. Fail loud on zero or more-than-one agents so
    the caller knows why the default is ambiguous.

    Used by ``tessera tokens create`` and ``tessera connect``; both
    share the "one agent = auto-select, many = disambiguate" contract
    per the P14 demo-script ergonomic fixes.
    """

    if explicit is not None:
        return explicit
    rows = conn.execute("SELECT id FROM agents ORDER BY id").fetchall()
    if not rows:
        raise CliError("vault has no agents; run `tessera agents create --vault X --name Y` first")
    if len(rows) > 1:
        ids = ", ".join(str(r[0]) for r in rows)
        raise CliError(f"vault has {len(rows)} agents ({ids}); pass --agent-id to pick one")
    return int(rows[0][0])


__all__ = [
    "CliError",
    "fail",
    "open_vault",
    "resolve_agent_id",
    "resolve_passphrase",
]
