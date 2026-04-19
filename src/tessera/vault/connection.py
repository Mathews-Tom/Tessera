"""Open and validate an encrypted Tessera vault.

``VaultConnection`` is a thin wrapper around ``sqlcipher3.Connection`` that:

* unlocks the vault with a :class:`~tessera.vault.encryption.ProtectedKey`
* applies the baseline PRAGMAs (``foreign_keys``, ``journal_mode = WAL``)
* verifies ``_meta.schema_version`` against the binary's expected version and
  ``_meta.schema_target`` for the Case-D in-transit state per
  :doc:`docs/migration-contract` §Version state machine
* rejects a Case-C vault (binary older than vault schema) and flags Case-A
  (forward migration required) via :class:`NeedsMigrationError`

The class does not run migrations — that work lives in
:mod:`tessera.migration`. Callers unlock, observe the state, and invoke the
migration runner explicitly per the contract's "no auto-migrate" rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import sqlcipher3

from tessera.vault.encryption import ProtectedKey
from tessera.vault.schema import SCHEMA_VERSION, iter_pragmas

BINARY_SCHEMA_VERSION: Final[int] = SCHEMA_VERSION


class VaultError(Exception):
    """Base class for vault-open failures."""


class VaultLockedError(VaultError):
    """Raised when the passphrase-derived key does not unlock the vault."""


class SchemaTooNewError(VaultError):
    """Case C: vault schema is newer than this binary supports."""


class NeedsMigrationError(VaultError):
    """Case A: vault schema is older than this binary; migration required."""


class MigrationInterruptedError(VaultError):
    """Case D: a prior migration left the vault in-transit."""


class VaultNotInitializedError(VaultError):
    """The vault file exists but has no ``_meta.schema_version`` row."""


@dataclass(frozen=True, slots=True)
class VaultState:
    schema_version: int
    schema_target: int | None
    vault_id: str
    kdf_version: int


class VaultConnection:
    """Unlocked handle to the sqlcipher-encrypted vault.

    Created via :meth:`open` (validates schema state) or :meth:`open_raw`
    (skips validation; used by the migration runner during bootstrap).
    """

    __slots__ = ("_closed", "_conn", "_state")

    def __init__(self, conn: sqlcipher3.Connection, state: VaultState | None) -> None:
        self._conn = conn
        self._state = state
        self._closed = False

    @classmethod
    def open(cls, path: Path, key: ProtectedKey) -> Self:
        conn = cls._unlock(path, key)
        try:
            state = _read_state(conn)
            _check_state(state)
        except VaultError:
            conn.close()
            raise
        return cls(conn, state)

    @classmethod
    def open_raw(cls, path: Path, key: ProtectedKey) -> Self:
        conn = cls._unlock(path, key)
        return cls(conn, state=None)

    @staticmethod
    def _unlock(path: Path, key: ProtectedKey) -> sqlcipher3.Connection:
        conn = sqlcipher3.connect(str(path), isolation_level=None)
        conn.execute(f"PRAGMA key = {key.as_pragma_literal()}")
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher3.DatabaseError as exc:
            conn.close()
            raise VaultLockedError(f"could not unlock vault at {path}: {exc}") from exc
        for pragma in iter_pragmas():
            conn.execute(pragma)
        return conn

    @property
    def connection(self) -> sqlcipher3.Connection:
        self._check_open()
        return self._conn

    @property
    def state(self) -> VaultState:
        self._check_open()
        if self._state is None:
            raise VaultError("connection opened in raw mode; state was not loaded")
        return self._state

    def reload_state(self) -> VaultState:
        self._check_open()
        self._state = _read_state(self._conn)
        return self._state

    def close(self) -> None:
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise VaultError("VaultConnection has been closed")


def _read_state(conn: sqlcipher3.Connection) -> VaultState:
    try:
        raw_rows = conn.execute(
            "SELECT key, value FROM _meta WHERE key IN (?, ?, ?, ?)",
            ("schema_version", "schema_target", "vault_id", "kdf_version"),
        ).fetchall()
    except sqlcipher3.OperationalError as exc:
        raise VaultNotInitializedError(f"_meta table missing ({exc})") from exc
    rows = {str(k): str(v) for k, v in raw_rows}
    if "schema_version" not in rows:
        raise VaultNotInitializedError("_meta.schema_version missing")
    schema_version = _as_int("schema_version", rows["schema_version"])
    schema_target = (
        _as_int("schema_target", rows["schema_target"])
        if rows.get("schema_target") not in (None, "")
        else None
    )
    vault_id = rows.get("vault_id", "")
    if not vault_id:
        raise VaultNotInitializedError("_meta.vault_id missing")
    kdf_version = _as_int("kdf_version", rows.get("kdf_version", "1"))
    return VaultState(
        schema_version=schema_version,
        schema_target=schema_target,
        vault_id=vault_id,
        kdf_version=kdf_version,
    )


def _check_state(state: VaultState) -> None:
    if state.schema_target is not None:
        raise MigrationInterruptedError(
            f"vault is in-transit to schema {state.schema_target}; "
            "run 'tessera vault recover' to resume or rollback"
        )
    if state.schema_version > BINARY_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"vault schema version {state.schema_version} is newer than "
            f"binary support ({BINARY_SCHEMA_VERSION}); upgrade tessera"
        )
    if state.schema_version < BINARY_SCHEMA_VERSION:
        raise NeedsMigrationError(
            f"vault schema {state.schema_version} requires migration to {BINARY_SCHEMA_VERSION}"
        )


def _as_int(key: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise VaultError(f"_meta.{key} is not an integer: {raw!r}") from exc


@contextmanager
def savepoint(conn: sqlcipher3.Connection, name: str) -> Iterator[None]:
    """Scope a block of DML inside a SQLite SAVEPOINT.

    SAVEPOINT (not BEGIN) works whether the caller is already inside a
    transaction — pysqlite's legacy auto-begin mode used by unit tests — or
    running against the autocommit ``VaultConnection`` used in production.
    Rolls back to and releases the savepoint on exception; releases on clean
    exit.
    """

    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")
