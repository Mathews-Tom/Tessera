"""Retrieval pipeline orchestrator.

Wires the per-stage modules — BM25, dense, RRF, SWCR (pass-through in
P4), cross-encoder rerank, MMR, token budget — into one async call that
``recall`` and ``assume_identity`` will sit on top of. Per-stage timing
is collected so the P8 MCP surface and P11 observability can surface
slow-query events per ``docs/determinism-and-observability.md``.

The SWCR reweighting stage is a no-op pass-through in P4. P5 replaces
``_swcr_passthrough`` with the real algorithm from ``docs/swcr-spec.md``.
Keeping the stage present but identity-valued here means the P5 landing
touches one function, not the pipeline's shape.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

import sqlcipher3

from tessera.adapters.protocol import Embedder, Reranker
from tessera.retrieval import bm25, budget, dense, mmr, rerank, rrf, seed
from tessera.vault import audit


@dataclass(frozen=True, slots=True)
class RecallMatch:
    external_id: str
    facet_type: str
    snippet: str
    score: float
    captured_at: int
    rank: int
    token_count: int


@dataclass(frozen=True, slots=True)
class RecallResult:
    matches: tuple[RecallMatch, ...]
    total_found: int
    warnings: tuple[str, ...]
    stage_ms: dict[str, float]
    seed: int
    rerank_degraded: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Fixed shape of everything the pipeline needs, passed as one object.

    Declared frozen so the config cannot drift between stages within a
    single call; mutation belongs at the pipeline-entry boundary.
    """

    conn: sqlcipher3.Connection
    embedder: Embedder
    reranker: Reranker
    active_model_id: int
    vec_table: str
    vault_id: str
    agent_id: int
    config: seed.RetrievalConfig
    tool_budget_tokens: int
    k: int
    facet_types: tuple[str, ...]
    candidates_per_list: int = 50


async def recall(ctx: PipelineContext, *, query_text: str) -> RecallResult:
    """Run the full retrieval pipeline for a single query.

    Audit correctness contract: every call writes exactly one
    ``retrieval_executed`` row, even on mid-pipeline exception. The
    row lands inside a ``try/finally`` so a crash in MMR or budget
    does not leave a gap in the forensic trail.
    """

    stage_ms: dict[str, float] = {}
    call_seed = seed.compute_seed(
        query_text=query_text,
        vault_id=ctx.vault_id,
        active_embedding_model_id=ctx.active_model_id,
        config=ctx.config,
    )
    bm25_lists: dict[str, list[bm25.BM25Candidate]] = {}
    dense_lists: dict[str, list[dense.DenseCandidate]] = {}
    reweighted: list[rrf.RRFResult] = []
    rerank_outcome = rerank.RerankOutcome(results=[], degraded=False, error_message=None)
    matches: tuple[RecallMatch, ...] = ()
    truncated = False
    pipeline_error: str | None = None

    try:
        # Stage 1 — hybrid candidate generation per facet type.
        t0 = time.perf_counter()
        bm25_lists, dense_lists = await _gather_candidates(ctx, query_text)
        stage_ms["candidates"] = _elapsed_ms(t0)

        # Stage 2 — RRF fusion. Per-type ranks preserved deliberately so
        # a doc at rank 0 in two lists accumulates a larger fused score
        # than a doc that only appears once.
        t0 = time.perf_counter()
        flat_bm25 = _flatten_bm25(bm25_lists)
        flat_dense = _flatten_dense(dense_lists)
        fused = rrf.fuse(flat_bm25, flat_dense)
        stage_ms["rrf"] = _elapsed_ms(t0)

        # Stage 3 — SWCR reweight (pass-through at P4).
        t0 = time.perf_counter()
        reweighted = _swcr_passthrough(fused)
        stage_ms["swcr"] = _elapsed_ms(t0)

        # Stage 4 — cross-encoder rerank.
        content_lookup = _content_lookup(bm25_lists, dense_lists)
        rerank_input = [
            (item.facet_id, content_lookup.get(item.facet_id, ""))
            for item in reweighted
            if item.facet_id in content_lookup
        ]
        t0 = time.perf_counter()
        rerank_outcome = await rerank.rerank(
            ctx.reranker,
            query_text=query_text,
            candidates=rerank_input,
            seed=call_seed,
        )
        stage_ms["rerank"] = _elapsed_ms(t0)
        if rerank_outcome.degraded:
            audit.write(
                ctx.conn,
                op="retrieval_rerank_degraded",
                actor="retrieval",
                agent_id=ctx.agent_id,
                payload={
                    "seed": seed.seed_hex(call_seed),
                    "reranker_name": type(ctx.reranker).__name__,
                    "reason": rerank_outcome.error_message or "unknown",
                },
            )

        # Stage 5 — MMR diversification.
        t0 = time.perf_counter()
        mmr_input = await _build_mmr_input(ctx, rerank_outcome.results, content_lookup)
        diversified = mmr.diversify(
            mmr_input,
            k=min(ctx.k * 3, len(mmr_input)),  # over-select; budget trims below
            mmr_lambda=ctx.config.mmr_lambda,
        )
        stage_ms["mmr"] = _elapsed_ms(t0)

        # Stage 6 — token-budget enforcement.
        t0 = time.perf_counter()
        items, truncated = _apply_budget(ctx, diversified, content_lookup)
        stage_ms["budget"] = _elapsed_ms(t0)

        matches = tuple(_to_matches(items, bm25_lists, dense_lists))[: ctx.k]
        if len(matches) < len(items):
            truncated = True
    except Exception as exc:
        pipeline_error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        audit.write(
            ctx.conn,
            op="retrieval_executed",
            actor="retrieval",
            agent_id=ctx.agent_id,
            payload={
                "seed": seed.seed_hex(call_seed),
                "retrieval_mode": ctx.config.retrieval_mode,
                "facet_types": list(ctx.facet_types),
                "k": ctx.k,
                "duration_ms": sum(stage_ms.values()),
                "stage_ms": dict(stage_ms),
                "candidate_counts": {
                    "bm25": sum(len(lst) for lst in bm25_lists.values()),
                    "dense": sum(len(lst) for lst in dense_lists.values()),
                    "fused": len(reweighted),
                },
                "result_count": len(matches),
                "result_facet_ids": [m.external_id for m in matches],
                "rerank_degraded": rerank_outcome.degraded,
                "truncated": truncated,
                "pipeline_error": pipeline_error,
            },
        )

    total_found = len({r.facet_id for r in reweighted})
    warnings = _warnings(rerank_outcome.degraded, truncated)
    return RecallResult(
        matches=matches,
        total_found=total_found,
        warnings=warnings,
        stage_ms=dict(stage_ms),
        seed=call_seed,
        rerank_degraded=rerank_outcome.degraded,
        truncated=truncated,
    )


async def _gather_candidates(
    ctx: PipelineContext, query_text: str
) -> tuple[dict[str, list[bm25.BM25Candidate]], dict[str, list[dense.DenseCandidate]]]:
    # sqlcipher3 connections are not thread-safe, so BM25 runs on the event
    # loop thread. Dense search awaits on the embedder (HTTP) and then runs
    # the vec query on the same connection — kept sequential per facet
    # type, parallelised only at the embedder-http layer via asyncio.gather
    # over the dense tasks (each dense call serialises its own DB access).
    bm25_by_type: dict[str, list[bm25.BM25Candidate]] = {}
    for ftype in ctx.facet_types:
        bm25_by_type[ftype] = bm25.search(
            ctx.conn,
            query_text=query_text,
            agent_id=ctx.agent_id,
            facet_type=ftype,
            limit=ctx.candidates_per_list,
        )
    dense_by_type: dict[str, list[dense.DenseCandidate]] = {}
    for ftype in ctx.facet_types:
        dense_by_type[ftype] = await dense.search(
            ctx.conn,
            embedder=ctx.embedder,
            vec_table=ctx.vec_table,
            query_text=query_text,
            agent_id=ctx.agent_id,
            facet_type=ftype,
            limit=ctx.candidates_per_list,
        )
    return bm25_by_type, dense_by_type


def _flatten_bm25(
    lists_by_type: dict[str, list[bm25.BM25Candidate]],
) -> list[bm25.BM25Candidate]:
    flat: list[bm25.BM25Candidate] = []
    for items in lists_by_type.values():
        flat.extend(items)
    return flat


def _flatten_dense(
    lists_by_type: dict[str, list[dense.DenseCandidate]],
) -> list[dense.DenseCandidate]:
    flat: list[dense.DenseCandidate] = []
    for items in lists_by_type.values():
        flat.extend(items)
    return flat


def _swcr_passthrough(fused: list[rrf.RRFResult]) -> list[rrf.RRFResult]:
    """P4 pass-through. P5 replaces this with the real SWCR algorithm."""

    return fused


def _iter_all_candidates(
    bm25_by_type: dict[str, list[bm25.BM25Candidate]],
    dense_by_type: dict[str, list[dense.DenseCandidate]],
) -> Iterator[bm25.BM25Candidate | dense.DenseCandidate]:
    # Both Candidate types share facet_id / external_id / facet_type /
    # content, so downstream indexers can treat them uniformly. Order is
    # BM25 first, then dense, so setdefault-style indexers preserve the
    # BM25-wins-on-tie behaviour expected by _content_lookup / _to_matches.
    for bm25_rows in bm25_by_type.values():
        yield from bm25_rows
    for dense_rows in dense_by_type.values():
        yield from dense_rows


def _content_lookup(
    bm25_by_type: dict[str, list[bm25.BM25Candidate]],
    dense_by_type: dict[str, list[dense.DenseCandidate]],
) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in _iter_all_candidates(bm25_by_type, dense_by_type):
        out.setdefault(row.facet_id, row.content)
    return out


async def _build_mmr_input(
    ctx: PipelineContext,
    reranked: list[rerank.RerankedCandidate],
    content_lookup: dict[int, str],
) -> list[mmr.MMRItem]:
    contents = [content_lookup.get(c.facet_id, "") for c in reranked]
    if not contents:
        return []
    embeddings = await ctx.embedder.embed(contents)
    return [
        mmr.MMRItem(
            facet_id=candidate.facet_id,
            relevance=candidate.score,
            embedding=embedding,
        )
        for candidate, embedding in zip(reranked, embeddings, strict=True)
    ]


def _apply_budget(
    ctx: PipelineContext,
    diversified: list[mmr.MMRResult],
    content_lookup: dict[int, str],
) -> tuple[tuple[budget.BudgetedItem, ...], bool]:
    items: list[budget.BudgetedItem] = []
    for candidate in diversified:
        raw = content_lookup.get(candidate.facet_id, "")
        snippet = budget.truncate_snippet(raw)
        items.append(
            budget.BudgetedItem(
                key=str(candidate.facet_id),
                snippet=snippet,
                token_count=budget.count_tokens(snippet),
            )
        )
    result = budget.apply_budget(items, total_budget=ctx.tool_budget_tokens)
    return result.items, result.truncated


def _to_matches(
    items: tuple[budget.BudgetedItem, ...],
    bm25_by_type: dict[str, list[bm25.BM25Candidate]],
    dense_by_type: dict[str, list[dense.DenseCandidate]],
) -> list[RecallMatch]:
    # Build a per-facet metadata index over the candidate pool so we can
    # emit the user-facing fields without a second DB round-trip.
    # captured_at is not on the Candidate dataclasses yet; the P4 pipeline
    # returns facet_type + external_id for the MCP surface and leaves
    # captured_at 0. P8 will enrich as needed.
    meta: dict[int, tuple[str, str, int]] = {}
    for row in _iter_all_candidates(bm25_by_type, dense_by_type):
        meta.setdefault(row.facet_id, (row.external_id, row.facet_type, 0))
    matches: list[RecallMatch] = []
    for rank_idx, item in enumerate(items):
        facet_id = int(item.key)
        if facet_id not in meta:
            continue
        external_id, facet_type, captured_at = meta[facet_id]
        matches.append(
            RecallMatch(
                external_id=external_id,
                facet_type=facet_type,
                snippet=item.snippet,
                score=1.0 / (1 + rank_idx),
                captured_at=captured_at,
                rank=rank_idx,
                token_count=item.token_count,
            )
        )
    return matches


def _warnings(rerank_degraded: bool, truncated: bool) -> tuple[str, ...]:
    warnings: list[str] = []
    if rerank_degraded:
        warnings.append("reranker_degraded: falling back to RRF order")
    if truncated:
        warnings.append("token_budget_truncated")
    return tuple(warnings)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0
