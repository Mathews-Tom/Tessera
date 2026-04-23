"""Daemon runtime state: unlocked vault + live retrieval pipeline.

Holds the one long-lived :class:`VaultConnection` and the reusable
:class:`PipelineContext` every MCP request shares. Both are created
once at daemon startup and torn down at shutdown; nothing in the
request path should swap them underneath a running call.

The state deliberately does not own the embed worker loop — that runs
as an :class:`asyncio.Task` under the daemon's lifecycle manager so a
stuck worker cannot block a graceful shutdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import sqlcipher3

from tessera.adapters import models_registry
from tessera.adapters.ollama_embedder import OllamaEmbedder
from tessera.adapters.protocol import Embedder, Reranker
from tessera.observability.events import EventLog
from tessera.retrieval.pipeline import PipelineContext
from tessera.retrieval.seed import DEFAULT_RETRIEVAL_MODE, RetrievalConfig
from tessera.vault.connection import VaultConnection
from tessera.vault.encryption import ProtectedKey


@dataclass(frozen=True, slots=True)
class DaemonState:
    """The handles every MCP request and control-plane call consults."""

    vault_path: Path
    vault: VaultConnection
    embedder: Embedder
    reranker: Reranker
    active_model_id: int
    vec_table: str
    vault_id: str
    event_log: EventLog | None = None


def open_vault_for_daemon(path: Path, key: ProtectedKey) -> VaultConnection:
    """Open the vault, rejecting Case C and Case D per migration contract.

    ``docs/migration-contract.md`` requires the daemon to refuse to
    start on a vault whose schema is newer than the binary supports
    (Case C) or mid-migration (Case D); ``VaultConnection.open``
    already raises the correct exception classes for both. The daemon
    simply refuses to catch them.
    """

    return VaultConnection.open(path, key)


def build_pipeline_context(
    state: DaemonState,
    *,
    agent_id: int,
    tool_budget_tokens: int,
    k: int,
    facet_types: tuple[str, ...],
) -> PipelineContext:
    """Project a :class:`DaemonState` into a per-request pipeline context."""

    return PipelineContext(
        conn=state.vault.connection,
        embedder=state.embedder,
        reranker=state.reranker,
        active_model_id=state.active_model_id,
        vec_table=state.vec_table,
        vault_id=state.vault_id,
        agent_id=agent_id,
        config=RetrievalConfig(
            rerank_model=state.reranker.model_name,
            mmr_lambda=0.7,
            max_candidates=100,
            retrieval_mode=DEFAULT_RETRIEVAL_MODE,
        ),
        tool_budget_tokens=tool_budget_tokens,
        k=k,
        facet_types=facet_types,
        # B-RET-2 sweep at 10K facets on the reference hardware baseline
        # (see docs/benchmarks/B-RET-2-recall-latency/results/) showed the
        # cross-encoder scales linearly with candidate pool; capping the
        # rerank input at 20 cuts p50 latency by ~35% vs the unbounded
        # fused list with no observed quality regression in B-RET-1.
        rerank_candidate_limit=20,
        event_log=state.event_log,
    )


def resolve_embedder(conn: sqlcipher3.Connection, *, ollama_host: str) -> tuple[Embedder, int, str]:
    """Return (embedder, active_model_id, vec_table) for the active model.

    Raises :class:`~tessera.adapters.models_registry.NoActiveModelError`
    when the vault has no row with ``is_active=1``; the daemon cannot
    serve ``recall`` without one and refuses to start.
    """

    model = models_registry.active_model(conn)
    # Only ollama is wired at v0.1; additional adapters add branches
    # here as they come online.
    if model.name != "ollama":
        raise ValueError(f"active embedding model {model.name!r} has no daemon-side adapter")
    embedder = OllamaEmbedder(model_name=model.name, dim=model.dim, host=ollama_host)
    return embedder, model.id, models_registry.vec_table_name(model.id)


__all__ = [
    "DaemonState",
    "build_pipeline_context",
    "open_vault_for_daemon",
    "resolve_embedder",
]
