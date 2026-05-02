"""Capture orchestrator — the synchronous write path for new facets.

The MCP ``capture`` tool and the ``tessera capture`` CLI both land here.
Capture is deliberately synchronous and cheap: validation + dedup +
insert + audit, all inside one transaction, with the expensive step
(embedding the content) punted to the async embed worker. This is what
lets ``capture`` return under the ``p95 < 50 ms`` DoD ceiling
regardless of how slow the embedder is.

Dedup semantics: two captures of the same normalized content against
the same agent collapse to one facet by the
``UNIQUE(agent_id, content_hash)`` constraint. A soft-deleted match is
restored rather than treated as new — re-capturing content the user
previously removed is an intentional un-delete, not a silent collision
with a tombstone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlcipher3

from tessera.vault import audit, compiled, facets


@dataclass(frozen=True, slots=True)
class CaptureResult:
    external_id: str
    is_duplicate: bool
    volatility: str
    ttl_seconds: int | None


def capture(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_type: str,
    content: str,
    source_tool: str,
    metadata: dict[str, Any] | None = None,
    captured_at: int | None = None,
    volatility: str = "persistent",
    ttl_seconds: int | None = None,
) -> CaptureResult:
    """Insert a facet and write the matching audit entry.

    Raises :class:`~tessera.vault.facets.UnsupportedFacetTypeError` for facet
    types outside the v0.1 set,
    :class:`~tessera.vault.facets.UnsupportedVolatilityError` /
    :class:`~tessera.vault.facets.InvalidTTLError` for ADR-0016 lifecycle
    misuses, and :class:`~tessera.vault.facets.UnknownAgentError` when
    ``agent_id`` does not correspond to a live ``agents`` row.
    """

    effective_ttl = facets.resolve_ttl_seconds(volatility, ttl_seconds)
    # Detect the un-delete branch of facets.insert before the call so
    # the V0.5-P6 staleness hook can gate on it. ``facets.insert`` has
    # three branches under content-hash dedup — brand-new (fresh
    # ULID), un-delete (soft-deleted match restored), and
    # live-duplicate (already-live match, no SQL mutation). Only the
    # un-delete branch is a genuine source-state change for any
    # Playbook citing the facet's external_id; flipping dependents on
    # a live-duplicate would invert the "no change → no stale flip"
    # invariant the soft-delete and skill paths uphold.
    digest = facets.content_hash(content)
    prior = conn.execute(
        "SELECT is_deleted FROM facets WHERE agent_id = ? AND content_hash = ?",
        (agent_id, digest),
    ).fetchone()
    was_undeleted = prior is not None and bool(prior[0])
    external_id, is_new = facets.insert(
        conn,
        agent_id=agent_id,
        facet_type=facet_type,
        content=content,
        source_tool=source_tool,
        metadata=metadata,
        captured_at=captured_at,
        volatility=volatility,
        ttl_seconds=effective_ttl,
    )
    audit.write(
        conn,
        op="facet_inserted",
        actor=source_tool,
        agent_id=agent_id,
        target_external_id=external_id,
        payload={
            "facet_type": facet_type,
            "source_tool": source_tool,
            "is_duplicate": not is_new,
            "content_hash_prefix": digest[:8],
            "volatility": volatility,
            "ttl_seconds": effective_ttl,
        },
    )
    # V0.5-P6 staleness wiring (ADR 0019 §Rationale 6). Only the
    # un-delete branch fires the cascade: a brand-new capture mints
    # a fresh ULID that cannot be cited yet (cascade would walk an
    # empty result set, harmless but noise), and a live-duplicate
    # re-capture is a no-op against an unchanged source row (firing
    # would wrongly flip dependents under "no change → no flip").
    # Only the un-delete branch represents a genuine source-state
    # change a citing Playbook should learn about — the prior
    # ``is_deleted = 1`` snapshot taken before ``facets.insert``
    # restores the row is what gates the call.
    if was_undeleted:
        compiled.mark_stale_for_source(
            conn,
            source_external_id=external_id,
            source_op="facet_inserted",
            agent_id=agent_id,
        )
    return CaptureResult(
        external_id=external_id,
        is_duplicate=not is_new,
        volatility=volatility,
        ttl_seconds=effective_ttl,
    )
