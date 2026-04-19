"""Dense (vector) candidate generator via sqlite-vec.

Queries the per-model ``vec_<active_id>`` virtual table for the top-``k``
nearest neighbours to a query vector, then joins back to ``facets`` to
filter by agent / facet_type / non-deleted. sqlite-vec's ``MATCH``
operator accepts the query vector serialised as packed little-endian
float32 bytes — the same shape the embed worker writes into the table
— so the caller never converts into an intermediate JSON form.

The embedder adapter produces the query vector — the caller passes in an
already-instantiated ``Embedder`` rather than this module picking one.
That keeps adapter lifecycle (keyring load, daemon warm-up) entirely in
the caller's hands and makes this module purely a SQL bridge.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import sqlcipher3

from tessera.adapters.protocol import Embedder
from tessera.vault.connection import ensure_vec_loaded


@dataclass(frozen=True, slots=True)
class DenseCandidate:
    facet_id: int
    external_id: str
    facet_type: str
    content: str
    distance: float
    rank: int


async def search(
    conn: sqlcipher3.Connection,
    *,
    embedder: Embedder,
    vec_table: str,
    query_text: str,
    agent_id: int,
    facet_type: str,
    limit: int = 50,
) -> list[DenseCandidate]:
    """Embed ``query_text``, run a sqlite-vec knn query, return typed rows."""

    if limit <= 0:
        raise ValueError(f"limit must be positive; got {limit}")
    stripped = query_text.strip()
    if not stripped:
        return []
    ensure_vec_loaded(conn)
    vectors = await embedder.embed([stripped])
    if not vectors:
        return []
    query_vec = vectors[0]
    serialized = _serialize_vector(query_vec)
    rows = conn.execute(
        f"""
        SELECT f.id, f.external_id, f.facet_type, f.content, v.distance
        FROM {vec_table} AS v
        JOIN facets AS f ON f.id = v.facet_id
        WHERE v.embedding MATCH ?
          AND k = ?
          AND f.is_deleted = 0
          AND f.agent_id = ?
          AND f.facet_type = ?
        ORDER BY v.distance ASC, f.id ASC
        """,
        (serialized, limit, agent_id, facet_type),
    ).fetchall()
    return [
        DenseCandidate(
            facet_id=int(row[0]),
            external_id=str(row[1]),
            facet_type=str(row[2]),
            content=str(row[3]),
            distance=float(row[4]),
            rank=idx,
        )
        for idx, row in enumerate(rows)
    ]


def _serialize_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)
