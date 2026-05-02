"""Retrieval pipeline orchestrator.

Wires the per-stage modules — BM25, dense, RRF, cross-encoder rerank,
SWCR reweighting, MMR, token budget — into one async call that the
MCP ``recall`` tool sits on top of.

Stage ordering per ``docs/swcr-spec.md §Pipeline placement``:

    candidates → RRF → rerank → SWCR → MMR → budget

``ctx.config.retrieval_mode`` dispatches between three arms:

    rrf_only     : skip rerank, skip SWCR. MMR ingests RRF-fused scores.
    rerank_only  : rerank after RRF; skip SWCR. MMR ingests rerank scores.
    swcr         : rerank after RRF; SWCR reweights the rerank scores;
                   MMR ingests SWCR-ordered scores. Default-on per
                   ADR 0011.

The mode is a property of the ``RetrievalConfig`` hash so switching arms
invalidates the determinism seed — two different modes are not the same
retrieval and never produce the same result set even for the same query.

Per-stage timing is collected so the MCP surface and observability
layer can surface slow-query events per
``docs/determinism-and-observability.md``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import sqlcipher3

from tessera.adapters.protocol import Embedder, Reranker
from tessera.observability.events import EventLog
from tessera.retrieval import bm25, budget, dense, mmr, rerank, rrf, seed, swcr
from tessera.vault import audit, retrospectives

# Default slow-query threshold in milliseconds. The spec frames the
# threshold as "p99 baseline + 50%"; until a persistent p99 baseline
# lands in v0.1.x, a fixed ceiling of 1500 ms is the conservative
# default — B-RET-2 at 1K facets records p95 around 100 ms on fake
# adapters, so 1500 ms only fires on genuine outliers.
DEFAULT_SLOW_RECALL_MS = 1500.0
# Scores at or below this floor are treated as no reliable retrieval signal.
# The current rerank/SWCR paths produce positive scores for normal matches;
# zero-or-negative scores are a stable way for adapters/tests to indicate
# that a candidate should not be surfaced as user context.
RECALL_RELEVANCE_FLOOR = 0.0


class RecallDegradedReason(StrEnum):
    EMPTY_VAULT = "empty_vault"
    NO_SIGNAL_ABOVE_FLOOR = "no_signal_above_floor"


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
    degraded_reason: RecallDegradedReason | None
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
    rerank_candidate_limit: int | None = None
    swcr_params: swcr.SWCRParams = swcr.DEFAULT_PARAMS
    event_log: EventLog | None = None
    slow_threshold_ms: float = DEFAULT_SLOW_RECALL_MS
    source_tool: str | None = None


async def recall(ctx: PipelineContext, *, query_text: str) -> RecallResult:
    """Run the full retrieval pipeline for a single query.

    Audit correctness contract: every call writes exactly one
    ``retrieval_executed`` row, even on mid-pipeline exception.
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
    fused: list[rrf.RRFResult] = []
    ordered: list[_ScoredCandidate] = []
    rerank_outcome = rerank.RerankOutcome(results=[], degraded=False, error_message=None)
    matches: tuple[RecallMatch, ...] = ()
    truncated = False
    pipeline_error: str | None = None

    try:
        # Stage 1 — hybrid candidate generation per facet type.
        t0 = time.perf_counter()
        bm25_lists, dense_lists = await _gather_candidates(ctx, query_text)
        stage_ms["candidates"] = _elapsed_ms(t0)

        # Stage 2 — RRF fusion. Per-type ranks preserved deliberately.
        t0 = time.perf_counter()
        flat_bm25 = [row for lst in bm25_lists.values() for row in lst]
        flat_dense = [row for lst in dense_lists.values() for row in lst]
        fused = rrf.fuse(flat_bm25, flat_dense)
        stage_ms["rrf"] = _elapsed_ms(t0)

        content_lookup = _content_lookup(bm25_lists, dense_lists)
        type_lookup = _type_lookup(bm25_lists, dense_lists)

        # Stage 3 — cross-encoder rerank (skipped in rrf_only).
        mode = ctx.config.retrieval_mode
        if mode == "rrf_only":
            stage_ms["rerank"] = 0.0
            ordered = [
                _ScoredCandidate(facet_id=r.facet_id, score=_rrf_to_score(r.rank))
                for r in fused
                if r.facet_id in content_lookup
            ]
        else:
            rerank_input = [
                (item.facet_id, content_lookup.get(item.facet_id, ""))
                for item in fused
                if item.facet_id in content_lookup
            ]
            if ctx.rerank_candidate_limit is not None:
                rerank_input = rerank_input[: ctx.rerank_candidate_limit]
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
            ordered = [
                _ScoredCandidate(facet_id=r.facet_id, score=r.score) for r in rerank_outcome.results
            ]
        ordered = _apply_relevance_floor(ordered)

        # Embed the ordered working set once; SWCR and MMR both consume it.
        working_ids = [c.facet_id for c in ordered]
        embeddings = await _embed_working_set(ctx, working_ids, content_lookup)

        # Stage 4 — SWCR reweight (skipped unless mode == "swcr").
        t0 = time.perf_counter()
        if mode == "swcr":
            # ADR 0018: when the working set includes an agent_profile
            # facet, augment with the most recent N retrospectives whose
            # ``agent_ref`` matches that profile. Augmentation runs
            # before the SWCR graph build so retrospectives enter the
            # cross-type bonus the same way every other facet type does.
            await _augment_with_retrospectives(
                ctx,
                ordered=ordered,
                content_lookup=content_lookup,
                type_lookup=type_lookup,
                embeddings=embeddings,
            )
            working_ids = [c.facet_id for c in ordered]
            entities_lookup = _fetch_entities(ctx.conn, working_ids)
            volatility_lookup = _fetch_volatility(ctx.conn, working_ids)
            swcr_input = [
                swcr.SWCRCandidate(
                    facet_id=cand.facet_id,
                    rerank_score=cand.score,
                    embedding=embeddings[cand.facet_id],
                    facet_type=type_lookup.get(cand.facet_id, ""),
                    entities=entities_lookup.get(cand.facet_id, frozenset()),
                    volatility=volatility_lookup.get(cand.facet_id, _PERSISTENT)[0],
                    captured_at=volatility_lookup.get(cand.facet_id, _PERSISTENT)[1],
                    ttl_seconds=volatility_lookup.get(cand.facet_id, _PERSISTENT)[2],
                )
                for cand in ordered
                if cand.facet_id in embeddings
            ]
            swcr_now = _now_epoch()
            swcr_results = swcr.apply(swcr_input, params=ctx.swcr_params, now=swcr_now)
            ordered = [_ScoredCandidate(facet_id=r.facet_id, score=r.score) for r in swcr_results]
            ordered = _apply_relevance_floor(ordered)
        stage_ms["swcr"] = _elapsed_ms(t0)

        # Stage 5 — MMR diversification.
        t0 = time.perf_counter()
        mmr_input = [
            mmr.MMRItem(
                facet_id=cand.facet_id,
                relevance=cand.score,
                embedding=list(embeddings[cand.facet_id]),
            )
            for cand in ordered
            if cand.facet_id in embeddings
        ]
        diversified = mmr.diversify(
            mmr_input,
            k=min(ctx.k * 3, len(mmr_input)),
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
        # Only the exception class name travels into the audit payload.
        # str(exc) on AdapterResponseError can include upstream-format
        # excerpts that routinely echo the embedding input, and JSON-parse
        # errors from ``_fetch_entities`` can surface metadata fragments.
        # The audit log's §S4 no-content guarantee is categorical, not
        # "best effort with truncation".
        pipeline_error = type(exc).__name__
        raise
    finally:
        audit_payload: dict[str, Any] = {
            "seed": seed.seed_hex(call_seed),
            "retrieval_mode": ctx.config.retrieval_mode,
            "facet_types": list(ctx.facet_types),
            "k": ctx.k,
            "duration_ms": sum(stage_ms.values()),
            "stage_ms": dict(stage_ms),
            "candidate_counts": {
                "bm25": sum(len(lst) for lst in bm25_lists.values()),
                "dense": sum(len(lst) for lst in dense_lists.values()),
                "fused": len(fused),
            },
            "result_count": len(matches),
            "result_facet_ids": [m.external_id for m in matches],
            "rerank_degraded": rerank_outcome.degraded,
            "truncated": truncated,
            "pipeline_error": pipeline_error,
        }
        # The audit write lives in its own try/except so a bug here —
        # allowlist drift, sqlcipher IO error — cannot swallow the
        # in-flight pipeline exception. We log to stderr and propagate
        # the original; observability of the root cause matters more
        # than fault-free audit emission.
        try:
            audit.write(
                ctx.conn,
                op="retrieval_executed",
                actor="retrieval",
                agent_id=ctx.agent_id,
                payload=audit_payload,
            )
        except Exception as audit_exc:
            sys.stderr.write(f"retrieval_executed audit write failed: {type(audit_exc).__name__}\n")
        _maybe_emit_slow(
            ctx,
            stage_ms=stage_ms,
            matches=matches,
            rerank_degraded=rerank_outcome.degraded,
            truncated=truncated,
        )

    total_found = len({r.facet_id for r in fused})
    degraded_reason = _degraded_reason(
        ctx,
        matches=matches,
        total_found=total_found,
        truncated=truncated,
    )
    warnings = _warnings(rerank_outcome.degraded, truncated)
    return RecallResult(
        matches=matches,
        total_found=total_found,
        warnings=warnings,
        degraded_reason=degraded_reason,
        stage_ms=dict(stage_ms),
        seed=call_seed,
        rerank_degraded=rerank_outcome.degraded,
        truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    facet_id: int
    score: float


def _apply_relevance_floor(candidates: list[_ScoredCandidate]) -> list[_ScoredCandidate]:
    return [cand for cand in candidates if cand.score > RECALL_RELEVANCE_FLOOR]


def _degraded_reason(
    ctx: PipelineContext,
    *,
    matches: tuple[RecallMatch, ...],
    total_found: int,
    truncated: bool,
) -> RecallDegradedReason | None:
    if matches:
        return None
    # If an unrealistically tiny response budget dropped all otherwise-valid
    # matches, the call is already represented by truncated=True rather than a
    # low-signal degraded reason.
    if truncated and total_found > 0:
        return None
    if _live_facet_count(ctx.conn, agent_id=ctx.agent_id, facet_types=ctx.facet_types) == 0:
        return RecallDegradedReason.EMPTY_VAULT
    return RecallDegradedReason.NO_SIGNAL_ABOVE_FLOOR


def _live_facet_count(
    conn: sqlcipher3.Connection,
    *,
    agent_id: int,
    facet_types: tuple[str, ...],
) -> int:
    if not facet_types:
        return 0
    placeholders = ",".join("?" for _ in facet_types)
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM facets
        WHERE agent_id = ?
          AND is_deleted = 0
          AND facet_type IN ({placeholders})
        """,
        (agent_id, *facet_types),
    ).fetchone()
    return int(row[0]) if row is not None else 0


async def _gather_candidates(
    ctx: PipelineContext, query_text: str
) -> tuple[dict[str, list[bm25.BM25Candidate]], dict[str, list[dense.DenseCandidate]]]:
    # sqlcipher3 connections are not thread-safe, so BM25 runs on the event
    # loop thread. BM25 is synchronous and cheap relative to dense; keep
    # the serial loop.
    bm25_by_type: dict[str, list[bm25.BM25Candidate]] = {
        ftype: bm25.search(
            ctx.conn,
            query_text=query_text,
            agent_id=ctx.agent_id,
            facet_type=ftype,
            limit=ctx.candidates_per_list,
        )
        for ftype in ctx.facet_types
    }
    dense_by_type = await _gather_dense_by_type(ctx, query_text)
    return bm25_by_type, dense_by_type


async def _gather_dense_by_type(
    ctx: PipelineContext, query_text: str
) -> dict[str, list[dense.DenseCandidate]]:
    """Embed once, fan the vec queries out across facet types.

    Each call to :func:`dense.search` used to re-embed the same query
    text per facet type — N facet types meant N identical embedder
    round-trips, each adding fastembed inference latency to the
    critical path. Embedding once and reusing the vector collapses
    that to a single call; ``asyncio.gather`` then dispatches the
    per-type vec queries concurrently. The vec queries themselves are
    synchronous against the shared sqlcipher3 connection and will not
    truly parallelise on that bottleneck, but the structure stages the
    work correctly for a future per-type connection split and keeps
    the critical path free of serialised awaits.
    """

    stripped = query_text.strip()
    if not stripped:
        return {ftype: [] for ftype in ctx.facet_types}
    vectors = await ctx.embedder.embed([stripped])
    if not vectors:
        return {ftype: [] for ftype in ctx.facet_types}
    query_vec = vectors[0]
    results = await asyncio.gather(
        *(
            dense.search_with_vector(
                ctx.conn,
                query_vec=query_vec,
                vec_table=ctx.vec_table,
                agent_id=ctx.agent_id,
                facet_type=ftype,
                limit=ctx.candidates_per_list,
            )
            for ftype in ctx.facet_types
        )
    )
    return dict(zip(ctx.facet_types, results, strict=True))


def _iter_all_candidates(
    bm25_by_type: dict[str, list[bm25.BM25Candidate]],
    dense_by_type: dict[str, list[dense.DenseCandidate]],
) -> Iterator[bm25.BM25Candidate | dense.DenseCandidate]:
    # Both Candidate types share facet_id / external_id / facet_type /
    # content. Order is BM25 first, then dense, so setdefault-style
    # indexers preserve the BM25-wins-on-tie behaviour.
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


def _type_lookup(
    bm25_by_type: dict[str, list[bm25.BM25Candidate]],
    dense_by_type: dict[str, list[dense.DenseCandidate]],
) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in _iter_all_candidates(bm25_by_type, dense_by_type):
        out.setdefault(row.facet_id, row.facet_type)
    return out


def _rrf_to_score(rank: int) -> float:
    # RRF-only mode feeds a derived relevance into MMR. Using 1/(1+rank)
    # rather than RRF's raw score keeps the score monotone in rank with
    # values in (0, 1] — convenient for the MMR's relevance term which
    # mixes with cosine in [-1, 1].
    return 1.0 / (1.0 + rank)


async def _embed_working_set(
    ctx: PipelineContext,
    facet_ids: list[int],
    content_lookup: dict[int, str],
) -> dict[int, list[float]]:
    if not facet_ids:
        return {}
    contents = [content_lookup.get(fid, "") for fid in facet_ids]
    vectors = await ctx.embedder.embed(contents)
    return {fid: list(vec) for fid, vec in zip(facet_ids, vectors, strict=True)}


# Default tuple used when a candidate has no volatility row in the
# fetched lookup (defensive only — every facet row has volatility after
# the v3→v4 migration). Treating as persistent collapses ``freshness``
# to 1.0, matching the v0.4 behaviour.
_PERSISTENT: Final[tuple[str, int, int | None]] = ("persistent", 0, None)


def _fetch_volatility(
    conn: sqlcipher3.Connection, facet_ids: Iterable[int]
) -> dict[int, tuple[str, int, int | None]]:
    """Return ``{facet_id: (volatility, captured_at, ttl_seconds)}``.

    Used by the SWCR stage to weight each candidate's freshness term per
    ADR 0016. ``captured_at`` is the wall-clock epoch the row was
    written; ``ttl_seconds`` is the per-row override or NULL when the
    row is using the volatility default.
    """

    ids = list(facet_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, volatility, captured_at, ttl_seconds FROM facets WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    out: dict[int, tuple[str, int, int | None]] = {}
    for row in rows:
        facet_id = int(row[0])
        volatility = str(row[1]) if row[1] is not None else "persistent"
        captured_at = int(row[2]) if row[2] is not None else 0
        ttl_seconds = int(row[3]) if row[3] is not None else None
        out[facet_id] = (volatility, captured_at, ttl_seconds)
    return out


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def _fetch_entities(
    conn: sqlcipher3.Connection, facet_ids: Iterable[int]
) -> dict[int, frozenset[str]]:
    """Return ``{facet_id: frozenset(entities)}`` for the candidate set.

    Entities come from the ``metadata`` JSON column's top-level
    ``"entities"`` key (list of strings). v0.3 replaces this with the
    structured ``entity_mentions`` table per docs/system-design.md;
    until then, capture writers stash entities in metadata and the
    retrieval pipeline reads them back here.
    """

    ids = list(facet_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, metadata FROM facets WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    out: dict[int, frozenset[str]] = {}
    for row in rows:
        facet_id = int(row[0])
        raw = str(row[1]) if row[1] is not None else "{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            out[facet_id] = frozenset()
            continue
        entities = parsed.get("entities") if isinstance(parsed, dict) else None
        if isinstance(entities, list):
            out[facet_id] = frozenset(str(e) for e in entities if isinstance(e, str) and e)
        else:
            out[facet_id] = frozenset()
    return out


async def _augment_with_retrospectives(
    ctx: PipelineContext,
    *,
    ordered: list[_ScoredCandidate],
    content_lookup: dict[int, str],
    type_lookup: dict[int, str],
    embeddings: dict[int, list[float]],
) -> None:
    """ADR 0018 SWCR augmentation — pull recent retrospectives for any
    ``agent_profile`` candidate in the working set.

    Mutates ``ordered``, ``content_lookup``, ``type_lookup``, and
    ``embeddings`` in place so the SWCR build sees the augmented set
    uniformly. The retrospective rows enter at the originating
    profile's score so they sit at a comparable level in the SWCR
    graph; the cross-type bonus then upweights the
    ``agent_profile ↔ retrospective`` edge naturally.

    Soft no-op when ``retrospective_window=0`` (caller disabled
    augmentation), when no agent_profile facets are in the working
    set, or when no matching retrospectives exist.
    """

    window = ctx.config.retrospective_window
    if window <= 0:
        return
    profile_candidates = [
        cand for cand in ordered if type_lookup.get(cand.facet_id) == "agent_profile"
    ]
    if not profile_candidates:
        return
    profile_external_ids = _profile_external_ids(
        ctx.conn, [cand.facet_id for cand in profile_candidates]
    )
    seen_ids: set[int] = {cand.facet_id for cand in ordered}
    new_rows: list[tuple[int, str, str, float]] = []
    for cand in profile_candidates:
        external_id = profile_external_ids.get(cand.facet_id)
        if external_id is None:
            continue
        retros = retrospectives.recent_for_agent(
            ctx.conn,
            agent_id=ctx.agent_id,
            profile_external_id=external_id,
            limit=window,
        )
        for retro in retros:
            if retro.facet_id in seen_ids:
                continue
            seen_ids.add(retro.facet_id)
            new_rows.append((retro.facet_id, retro.content, "retrospective", cand.score))
    if not new_rows:
        return
    new_ids = [fid for fid, *_ in new_rows]
    new_contents = [content for _, content, *_ in new_rows]
    vectors = await ctx.embedder.embed(new_contents)
    for (fid, content, ftype, score), vec in zip(new_rows, vectors, strict=True):
        ordered.append(_ScoredCandidate(facet_id=fid, score=score))
        content_lookup[fid] = content
        type_lookup[fid] = ftype
        embeddings[fid] = list(vec)
    del new_ids


def _profile_external_ids(conn: sqlcipher3.Connection, facet_ids: Iterable[int]) -> dict[int, str]:
    """Return ``{facet_id: external_id}`` for ``agent_profile`` rows."""

    ids = list(facet_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, external_id FROM facets
        WHERE id IN ({placeholders}) AND facet_type = 'agent_profile'
              AND is_deleted = 0
        """,
        tuple(ids),
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


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
    meta: dict[int, tuple[str, str, int]] = {}
    for row in _iter_all_candidates(bm25_by_type, dense_by_type):
        # captured_at is not on the Candidate dataclasses yet; P8 will
        # enrich as needed for the MCP surface. 0 is a stable placeholder
        # that flags "unknown" without propagating null.
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


def _maybe_emit_slow(
    ctx: PipelineContext,
    *,
    stage_ms: dict[str, float],
    matches: tuple[RecallMatch, ...],
    rerank_degraded: bool,
    truncated: bool,
) -> None:
    """Emit a ``recall_slow`` event when the call exceeds the threshold.

    Payload contract (``docs/determinism-and-observability.md §Slow-query
    sampling``): seed, params, duration_ms, stage_breakdown_ms,
    candidate_counts_per_stage, source_tool. No query text, no result
    content — only what lets the operator reproduce against their own
    vault.
    """

    if ctx.event_log is None:
        return
    duration_ms = sum(stage_ms.values())
    if duration_ms < ctx.slow_threshold_ms:
        return
    ctx.event_log.emit(
        level="warn",
        category="retrieval",
        event="recall_slow",
        duration_ms=int(duration_ms),
        attrs={
            "facet_types": list(ctx.facet_types),
            "k": ctx.k,
            "retrieval_mode": ctx.config.retrieval_mode,
            "stage_ms": {k: round(v, 2) for k, v in stage_ms.items()},
            "result_count": len(matches),
            "rerank_degraded": rerank_degraded,
            "truncated": truncated,
            "source_tool": ctx.source_tool,
            "threshold_ms": ctx.slow_threshold_ms,
        },
    )
