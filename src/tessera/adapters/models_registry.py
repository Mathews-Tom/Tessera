"""DB-side embedding-model registry and per-model vec-table management.

This module is the bridge between the Python adapter registry
(``tessera.adapters.registry``) and the on-disk ``embedding_models`` table
introduced in the P1 schema. Registering an embedder here does two things in
one transaction:

1. Inserts a row into ``embedding_models`` so retrieval can look up
   ``(name, dim, is_active)`` by id.
2. Creates the per-model ``vec_<id>`` virtual table via ``sqlite-vec``, with
   ``dim`` baked in per ADR 0003.

The single-active invariant is enforced by the unique partial index on
``is_active = 1`` shipped with the P1 schema; the Python code sets the flag
inside a transaction that first clears every other row.

``sqlite-vec`` is loaded into the connection lazily because the extension has
to be loaded after sqlcipher has unlocked the page cache. Doing it inside
``VaultConnection.open`` would force vec to be available on every open — an
unnecessary coupling for code paths (backup, rollback, CLI inspect) that do
not touch vectors.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlcipher3
import sqlite_vec

from tessera.adapters.registry import list_embedders

VEC_TABLE_PREFIX: Final[str] = "vec_"


class ModelRegistryError(Exception):
    """Base class for on-disk model-registry failures."""


class DuplicateModelError(ModelRegistryError):
    """A row already exists for this model ``name``."""


class UnknownModelError(ModelRegistryError):
    """Requested model ``name`` or ``id`` is not registered in the vault."""


class NoActiveModelError(ModelRegistryError):
    """No embedding model is flagged ``is_active = 1``."""


@dataclass(frozen=True, slots=True)
class EmbeddingModel:
    id: int
    name: str
    dim: int
    added_at: int
    is_active: bool


def ensure_vec_loaded(conn: sqlcipher3.Connection) -> None:
    """Load the ``sqlite-vec`` extension into ``conn`` if not already loaded.

    Calling this on a connection that already has vec loaded is a no-op — the
    extension's ``vec_version()`` scalar function becomes available, which is
    how we detect the already-loaded case without keeping a per-connection
    flag outside the sqlite state.
    """

    try:
        conn.execute("SELECT vec_version()").fetchone()
        return
    except (sqlcipher3.OperationalError, sqlcipher3.DatabaseError):
        pass
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def register_embedding_model(
    conn: sqlcipher3.Connection,
    *,
    name: str,
    dim: int,
    activate: bool = False,
) -> EmbeddingModel:
    """Register an embedding model in the vault.

    Inserts a row into ``embedding_models``, creates the associated
    ``vec_<id>`` virtual table, and — when ``activate`` is set — promotes the
    new model to the single active slot in the same transaction.

    Raises :class:`DuplicateModelError` when ``name`` is already registered;
    the caller's idempotent recovery path should query :func:`get_by_name`
    first.
    """

    _check_name(name)
    if dim <= 0:
        raise ModelRegistryError(f"dim must be positive; got {dim}")
    if name not in _python_registry_embedders():
        raise ModelRegistryError(
            f"no python adapter registered for embedder {name!r}; "
            "import the adapter module before register_embedding_model"
        )
    ensure_vec_loaded(conn)
    # Duplicate detection runs before BEGIN so raising does not require a
    # rollback that would itself fail on an unopened transaction.
    existing = conn.execute("SELECT id FROM embedding_models WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        raise DuplicateModelError(f"embedding model {name!r} already registered")
    now = _now_epoch()
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO embedding_models(name, dim, added_at, is_active) VALUES (?, ?, ?, 0)",
            (name, dim, now),
        )
        model_id = cur.lastrowid
        if model_id is None:
            raise ModelRegistryError("INSERT into embedding_models produced no rowid")
        conn.execute(
            f"CREATE VIRTUAL TABLE {_vec_table(model_id)} "
            f"USING vec0(facet_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        if activate:
            _activate_in_transaction(conn, model_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return EmbeddingModel(
        id=int(model_id),
        name=name,
        dim=dim,
        added_at=now,
        is_active=activate,
    )


def activate(conn: sqlcipher3.Connection, *, name: str) -> EmbeddingModel:
    """Promote ``name`` to the active model slot. Idempotent."""

    model = get_by_name(conn, name)
    conn.execute("BEGIN")
    try:
        _activate_in_transaction(conn, model.id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return EmbeddingModel(
        id=model.id,
        name=model.name,
        dim=model.dim,
        added_at=model.added_at,
        is_active=True,
    )


def _activate_in_transaction(conn: sqlcipher3.Connection, model_id: int) -> None:
    # The unique partial index on is_active = 1 makes a single UPDATE that
    # toggles two rows at once reject; clear first, set second. Both writes
    # inside the caller's transaction so a crash between them leaves the
    # vault with zero active models rather than two (the partial index would
    # have rejected the offending write anyway).
    conn.execute("UPDATE embedding_models SET is_active = 0 WHERE is_active = 1")
    conn.execute("UPDATE embedding_models SET is_active = 1 WHERE id = ?", (model_id,))


def get_by_name(conn: sqlcipher3.Connection, name: str) -> EmbeddingModel:
    row = conn.execute(
        "SELECT id, name, dim, added_at, is_active FROM embedding_models WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise UnknownModelError(f"no embedding model registered under {name!r}")
    return _row_to_model(row)


def get_by_id(conn: sqlcipher3.Connection, model_id: int) -> EmbeddingModel:
    row = conn.execute(
        "SELECT id, name, dim, added_at, is_active FROM embedding_models WHERE id = ?",
        (model_id,),
    ).fetchone()
    if row is None:
        raise UnknownModelError(f"no embedding model with id {model_id}")
    return _row_to_model(row)


def list_models(conn: sqlcipher3.Connection) -> list[EmbeddingModel]:
    rows = conn.execute(
        "SELECT id, name, dim, added_at, is_active FROM embedding_models ORDER BY id"
    ).fetchall()
    return [_row_to_model(r) for r in rows]


def active_model(conn: sqlcipher3.Connection) -> EmbeddingModel:
    row = conn.execute(
        "SELECT id, name, dim, added_at, is_active FROM embedding_models WHERE is_active = 1"
    ).fetchone()
    if row is None:
        raise NoActiveModelError("no embedding model is flagged active")
    return _row_to_model(row)


def vec_table_name(model_id: int) -> str:
    return _vec_table(model_id)


def _vec_table(model_id: int) -> str:
    if model_id <= 0:
        raise ModelRegistryError(f"invalid model id {model_id}")
    return f"{VEC_TABLE_PREFIX}{model_id}"


def _row_to_model(row: Sequence[Any]) -> EmbeddingModel:
    return EmbeddingModel(
        id=int(row[0]),
        name=str(row[1]),
        dim=int(row[2]),
        added_at=int(row[3]),
        is_active=bool(row[4]),
    )


def _python_registry_embedders() -> frozenset[str]:
    # Forgetting to import an adapter module before register_embedding_model
    # produces a clean ModelRegistryError here, rather than a stale entry in
    # embedding_models the retrieval pipeline later cannot dispatch.
    return frozenset(list_embedders())


def _check_name(name: str) -> None:
    if not name:
        raise ModelRegistryError("embedding model name must be a non-empty string")
    # Names are used only as a lookup key against embedding_models.name and
    # are never interpolated into SQL. Per-model vec tables use the integer
    # id, not the name, so there is no injection risk here.


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
