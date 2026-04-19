"""Per-call deterministic seed generation.

``docs/determinism-and-observability.md §Retrieval pipeline determinism``
requires every retrieval stage that has a tie-break to resolve it from a
single shared seed, so 100 identical queries on a stable vault produce
bit-identical result IDs. The seed is derived from inputs that actually
vary (query text, vault identity, the active embedder, the current
retrieval config); changing any of them invalidates the determinism
guarantee in a way users can observe and reason about.

Downstream stages (RRF, MMR) use the seed as an RNG source when they need
random tie-breaking under ``deterministic=False``. Under
``deterministic=True`` (the default) the seed is still derived so that
replays from the audit log reconstruct the same call shape, but the tie-
break is purely ordinal (``facets.id`` ascending).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """The slice of retrieval config that influences determinism.

    Changing any field here intentionally invalidates the seed; a tuned
    parameter is a new retrieval regime even if the query text is the same.
    """

    rerank_model: str
    mmr_lambda: float
    max_candidates: int
    retrieval_mode: str  # 'rrf_only' | 'rerank_only' | 'swcr'

    def hash(self) -> str:
        payload = json.dumps(
            {
                "rerank_model": self.rerank_model,
                "mmr_lambda": self.mmr_lambda,
                "max_candidates": self.max_candidates,
                "retrieval_mode": self.retrieval_mode,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_seed(
    *,
    query_text: str,
    vault_id: str,
    active_embedding_model_id: int,
    config: RetrievalConfig,
) -> int:
    """Return a 64-bit seed derived from the inputs.

    The final cast to ``int`` ensures the seed fits ``torch.manual_seed``
    and Python's ``random.Random``; 64 bits is comfortably below both
    callers' upper bounds and matches the 8-byte prefix the spec calls for.
    """

    payload = json.dumps(
        {
            "q": query_text,
            "vault": vault_id,
            "embed_model_id": active_embedding_model_id,
            "config_hash": config.hash(),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def seed_hex(seed: int) -> str:
    """Format a seed for audit-log inclusion (stable across replays)."""

    return f"0x{seed:016x}"
