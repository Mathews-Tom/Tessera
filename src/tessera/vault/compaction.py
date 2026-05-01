"""Auto-compaction sweep for ADR 0016 memory volatility.

Idle-time pass that soft-deletes ``session`` and ``ephemeral`` rows
whose TTL has elapsed. Soft-delete reuses the existing
``facets.is_deleted`` path so the audit trail stays uniform with
manual ``forget`` operations; hard-delete cascade across per-model
``vec_<id>`` tables continues to run on the existing v0.1 hard-delete
schedule.

The sweep is deterministic given a fixed ``now`` so callers can pin
``datetime.now()`` in tests. It does not block writes — the lookup
runs against the partial ``facets_volatility_sweep`` index so the
common case (a vault dominated by ``persistent`` rows) sees no
contention.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import sqlcipher3

from tessera.vault import audit, facets

# Cap on the number of rows compacted in one sweep. Bounded so a long
# idle period that accumulated thousands of expired rows cannot stall
# the daemon's event loop. Subsequent sweeps drain the rest.
DEFAULT_BATCH_LIMIT: Final[int] = 256


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Per-sweep totals returned to the caller (daemon or tests)."""

    inspected: int
    compacted: int
    skipped: int


def sweep(
    conn: sqlcipher3.Connection,
    *,
    now: int | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> CompactionResult:
    """Soft-delete one batch of expired session/ephemeral rows.

    Returns a :class:`CompactionResult` summarising the sweep. Callers
    typically run this on a daemon idle tick; tests pin ``now`` to
    force determinism. Each soft-delete writes a ``facet_auto_compacted``
    audit row recording the row's facet type, volatility, and age in
    seconds.
    """

    sweep_now = now if now is not None else _now_epoch()
    expired = facets.list_expired_volatile(conn, now=sweep_now, limit=limit)
    if not expired:
        return CompactionResult(inspected=0, compacted=0, skipped=0)
    compacted = 0
    skipped = 0
    for facet in expired:
        if not facets.soft_delete(conn, facet.external_id):
            skipped += 1
            continue
        audit.write(
            conn,
            op="facet_auto_compacted",
            actor="auto_compaction",
            agent_id=facet.agent_id,
            target_external_id=facet.external_id,
            payload={
                "facet_type": facet.facet_type,
                "volatility": facet.volatility,
                "age_seconds": max(sweep_now - facet.captured_at, 0),
            },
        )
        compacted += 1
    return CompactionResult(inspected=len(expired), compacted=compacted, skipped=skipped)


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def expired_facet_ids(facet_iter: Iterable[facets.Facet], *, now: int) -> list[int]:
    """Return facet ids whose TTL has elapsed at ``now``.

    Convenience helper for callers that iterate a fixture-driven list
    rather than running a SQL fetch. Mirrors the SQL filter in
    :func:`tessera.vault.facets.list_expired_volatile`.
    """

    out: list[int] = []
    for facet in facet_iter:
        if facet.volatility == "persistent" or facet.is_deleted:
            continue
        ttl = facet.ttl_seconds
        if ttl is None:
            ttl = facets.DEFAULT_TTL_SECONDS.get(facet.volatility)
        if ttl is None or ttl <= 0:
            continue
        if facet.captured_at + ttl <= now:
            out.append(facet.id)
    return out


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "CompactionResult",
    "expired_facet_ids",
    "sweep",
]
