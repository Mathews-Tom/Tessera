"""Async embed worker.

Capture writes facets synchronously at ``embed_status='pending'``; this
module is the async consumer that eventually flips them to ``'embedded'``
(or ``'failed'`` after the retry cap). Keeping the worker a function-level
``run_pass`` rather than a daemon loop means the P9 daemon can own
scheduling (polling interval, starvation control, shutdown signalling)
without this module taking a hard dependency on the daemon runtime.

State transitions:

* ``pending`` + attempts < MAX → call embedder.
  * Success → write vec row, set ``embed_status='embedded'``, clear error.
  * Retryable error (network, OOM) and attempts < MAX → stay ``pending``,
    bump ``embed_attempts``, record ``embed_last_error`` /
    ``embed_last_attempt_at``. Next pass skips until backoff elapses.
  * Retryable error and attempts == MAX → flip to ``failed``, keep
    ``embed_last_error`` so ``tessera vault repair-embeds`` can surface it.
  * Terminal error (model-not-found, auth, response-shape) → flip to
    ``failed`` immediately, no further retries.
* ``failed`` → ignored by the worker; the user runs ``tessera vault
  repair-embeds`` to reset ``attempts=0`` and kick the facet back to
  ``pending``.

The query over-fetches then filters by backoff in Python so the backoff
logic stays in a single ``retry_policy.BACKOFF_SECONDS`` array rather than
inlined as a SQL CASE expression.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlcipher3

from tessera.adapters.errors import AdapterError
from tessera.adapters.protocol import Embedder
from tessera.retrieval.retry_policy import BACKOFF_SECONDS, MAX_ATTEMPTS, decide

DEFAULT_BATCH_SIZE = 16


@dataclass(frozen=True, slots=True)
class PassStats:
    embedded: int
    retrying: int
    failed: int
    skipped_backoff: int


async def run_pass(
    conn: sqlcipher3.Connection,
    embedder: Embedder,
    *,
    active_model_id: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now_epoch: int | None = None,
) -> PassStats:
    """Embed up to ``batch_size`` pending facets and record the result."""

    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive; got {batch_size}")
    now = now_epoch if now_epoch is not None else _now_epoch()
    vec_table = f"vec_{active_model_id}"
    candidates = _fetch_candidates(conn, limit=batch_size * 4)
    embedded = 0
    retrying = 0
    failed = 0
    skipped_backoff = 0
    processed = 0
    for facet_id, content, attempts, last_attempt_at in candidates:
        if processed >= batch_size:
            break
        if not _backoff_elapsed(attempts=attempts, last_attempt_at=last_attempt_at, now=now):
            skipped_backoff += 1
            continue
        processed += 1
        try:
            vectors = await embedder.embed([content])
        except AdapterError as exc:
            outcome = _record_failure(
                conn,
                facet_id=facet_id,
                attempts=attempts,
                error=exc,
                model_id=active_model_id,
                now=now,
            )
            if outcome == "retrying":
                retrying += 1
            else:
                failed += 1
            continue
        _record_success(
            conn,
            facet_id=facet_id,
            vector=vectors[0],
            vec_table=vec_table,
            model_id=active_model_id,
            now=now,
        )
        embedded += 1
    return PassStats(
        embedded=embedded,
        retrying=retrying,
        failed=failed,
        skipped_backoff=skipped_backoff,
    )


def _fetch_candidates(
    conn: sqlcipher3.Connection, *, limit: int
) -> Iterator[tuple[int, str, int, int | None]]:
    rows = conn.execute(
        """
        SELECT id, content, embed_attempts, embed_last_attempt_at
        FROM facets
        WHERE is_deleted = 0 AND embed_status = 'pending'
        ORDER BY captured_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _cast_candidate_rows(rows)


def _cast_candidate_rows(
    rows: Iterable[tuple[Any, ...]],
) -> Iterator[tuple[int, str, int, int | None]]:
    for row in rows:
        last = int(row[3]) if row[3] is not None else None
        yield int(row[0]), str(row[1]), int(row[2]), last


def _backoff_elapsed(*, attempts: int, last_attempt_at: int | None, now: int) -> bool:
    if attempts == 0 or last_attempt_at is None:
        return True
    idx = min(attempts - 1, len(BACKOFF_SECONDS) - 1)
    return (now - last_attempt_at) >= BACKOFF_SECONDS[idx]


def _record_success(
    conn: sqlcipher3.Connection,
    *,
    facet_id: int,
    vector: list[float],
    vec_table: str,
    model_id: int,
    now: int,
) -> None:
    serialized = _serialize_vector(vector)
    # Savepoint rather than BEGIN because the caller may be running against
    # a connection whose isolation mode auto-begins on the first DML (the
    # legacy pysqlite mode the unit tests use). On the autocommit
    # VaultConnection the savepoint is simply the transaction scope.
    conn.execute("SAVEPOINT embed_write")
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {vec_table}(facet_id, embedding) VALUES (?, ?)",
            (facet_id, serialized),
        )
        conn.execute(
            """
            UPDATE facets
            SET embed_status = 'embedded',
                embed_model_id = ?,
                embed_attempts = embed_attempts + 1,
                embed_last_attempt_at = ?,
                embed_last_error = NULL
            WHERE id = ?
            """,
            (model_id, now, facet_id),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT embed_write")
        conn.execute("RELEASE SAVEPOINT embed_write")
        raise
    conn.execute("RELEASE SAVEPOINT embed_write")


def _record_failure(
    conn: sqlcipher3.Connection,
    *,
    facet_id: int,
    attempts: int,
    error: AdapterError,
    model_id: int,
    now: int,
) -> str:
    new_attempts = attempts + 1
    decision = decide(error, new_attempts)
    should_retry = decision.should_retry and new_attempts < MAX_ATTEMPTS
    status = "pending" if should_retry else "failed"
    error_message = f"{type(error).__name__}: {error}"[:500]
    conn.execute(
        """
        UPDATE facets
        SET embed_status = ?,
            embed_model_id = ?,
            embed_attempts = ?,
            embed_last_attempt_at = ?,
            embed_last_error = ?
        WHERE id = ?
        """,
        (status, model_id, new_attempts, now, error_message, facet_id),
    )
    return "retrying" if should_retry else "failed"


def _serialize_vector(vector: list[float]) -> bytes:
    # sqlite-vec accepts float[] columns as a packed little-endian float32
    # blob. Using struct keeps the dependency set at stdlib.
    import struct

    return struct.pack(f"<{len(vector)}f", *vector)


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
