"""Sync watermark persistence for V0.5-P9b BYO sync.

Per ADR-0022 D3: ``last_restored_sequence`` lives as a row in the
existing ``_meta`` table, keyed by a stable hash of
``endpoint || bucket || prefix``. The watermark survives credential
rotation (creds are not part of the key), resets on bucket-change
(different store identity → different watermark), and inherits the
SQLCipher at-rest encryption automatically.

Why ``_meta`` and not a sidecar file: a sidecar that retained the
pre-restore watermark would refuse a legitimate restore-then-pull
flow ("you just restored from sequence N; the next pull from the
same store should not be ≤ N"). Watermark in ``_meta`` resets to
zero on restore, which matches operator intuition. ADR-0022 §D3
walks through the trade-off in full.

Why hash the store identity: a future operator might rotate the
access key without changing the bucket. Keying the watermark on
the credentials would restart the watermark each rotation, which
produces a one-pull regression every time. The store identity is
the *target* (endpoint + bucket + prefix), not the *credential*.
"""

from __future__ import annotations

import hashlib
from typing import Final

import sqlcipher3

_META_KEY_PREFIX: Final[str] = "sync_watermark_"
_STORE_ID_HASH_LENGTH: Final[int] = 32


class WatermarkError(Exception):
    """Base class for watermark persistence failures."""


class CorruptWatermarkError(WatermarkError):
    """The stored watermark value is not a non-negative integer.

    Distinct from a missing watermark (which is the legitimate
    fresh-vault state). A corrupt value indicates either a vault
    edit by an external tool or a serialization bug; the caller
    should surface this to the operator rather than silently
    resetting to zero (which would accept a replay of any
    previously-restored snapshot).
    """


def store_identity(*, endpoint: str, bucket: str, prefix: str) -> str:
    """Stable hash of the store target.

    Returns a 32-char hex prefix of sha256(``endpoint||bucket||prefix``).
    Truncating is safe because the hash is the *key* in a key-
    value table, not a security primitive — collisions across
    real-world store configurations are negligible at 128 bits of
    entropy. Truncating keeps the ``_meta.key`` column readable
    in a debugger session without scrolling.

    The endpoint, bucket, and prefix are normalised before
    hashing so cosmetic operator typos (trailing slashes, scheme
    case) do not produce different store identities.
    """

    norm_endpoint = endpoint.rstrip("/").lower()
    norm_bucket = bucket.strip()
    norm_prefix = prefix.strip("/")
    payload = f"{norm_endpoint}\x00{norm_bucket}\x00{norm_prefix}".encode()
    return hashlib.sha256(payload).hexdigest()[:_STORE_ID_HASH_LENGTH]


def meta_key_for(store_id: str) -> str:
    """Return the ``_meta.key`` value used for a store's watermark.

    Wrapped in a function so a future schema change (e.g., moving
    watermarks to a dedicated table) has one call site to update.
    """

    return f"{_META_KEY_PREFIX}{store_id}"


def read_watermark(conn: sqlcipher3.Connection, *, store_id: str) -> int:
    """Return the persisted watermark for ``store_id``.

    Returns 0 when no watermark row exists — the legitimate
    fresh-vault / first-pull state. Raises
    :class:`CorruptWatermarkError` when a row exists but the
    stored value cannot be parsed as a non-negative integer.

    The caller (the CLI's pull command) feeds the returned value
    into ``pull(last_restored_sequence=...)``. The sync.pull
    primitive enforces the replay-defence invariant; this module
    is only the persistence layer.
    """

    row = conn.execute(
        "SELECT value FROM _meta WHERE key = ?", (meta_key_for(store_id),)
    ).fetchone()
    if row is None:
        return 0
    raw = row[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CorruptWatermarkError(
            f"watermark for store {store_id!r} is not an integer: {raw!r}"
        ) from exc
    if value < 0:
        raise CorruptWatermarkError(f"watermark for store {store_id!r} is negative: {value}")
    return value


def write_watermark(conn: sqlcipher3.Connection, *, store_id: str, sequence: int) -> None:
    """Persist ``sequence`` as the new watermark for ``store_id``.

    The CLI's pull command calls this after a successful pull so
    the next pull's replay-defence sees the latest restored
    sequence. Sequence is required to be ≥ 1 — the value 0 is
    reserved for "no pull has happened yet" and is the implicit
    return of :func:`read_watermark` when no row exists.

    Uses ``INSERT OR REPLACE`` so the call is idempotent and
    monotonically updates the row in place. The schema's
    ``_meta`` table treats ``key`` as a primary key so concurrent
    writes from different code paths cannot create duplicate
    rows; the single-writer-per-vault invariant from V0.5-P8 is
    inherited here.
    """

    if sequence < 1:
        raise WatermarkError(f"sequence must be >= 1, got {sequence}")
    conn.execute(
        "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
        (meta_key_for(store_id), str(sequence)),
    )


def clear_watermark(conn: sqlcipher3.Connection, *, store_id: str) -> None:
    """Remove the persisted watermark for ``store_id``.

    Used by the CLI's reset / re-setup flows when the operator
    explicitly wants to start over against a known-clean store.
    Does not raise when no row exists (the post-condition is
    "no row for this store_id"; a no-op satisfies that).
    """

    conn.execute("DELETE FROM _meta WHERE key = ?", (meta_key_for(store_id),))


__all__ = [
    "CorruptWatermarkError",
    "WatermarkError",
    "clear_watermark",
    "meta_key_for",
    "read_watermark",
    "store_identity",
    "write_watermark",
]
