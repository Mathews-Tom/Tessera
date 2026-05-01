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

from tessera.vault import audit, facets


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
            "content_hash_prefix": facets.content_hash(content)[:8],
            "volatility": volatility,
            "ttl_seconds": effective_ttl,
        },
    )
    return CaptureResult(
        external_id=external_id,
        is_duplicate=not is_new,
        volatility=volatility,
        ttl_seconds=effective_ttl,
    )
