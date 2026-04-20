"""``assume_identity`` bundle assembler.

The single load-bearing MCP call the model-swap demo depends on: a fresh
substrate calls ``assume_identity()`` once, receives a curated bundle of
facets spanning voice samples, recent events, and (v0.3+) skills /
relationships / goals, and behaves continuously with the prior agent.

Design per ``docs/swcr-spec.md §Per-type budget enforcement`` + P6 plan:

1. Per-role parallel retrieval. Each role has its own facet_type filter,
   k_max cap, and sub-budget derived from the role's ``budget_fraction``.
2. Time-window filter for ``recent_events`` (episodic). Default 168 hours.
3. Bundle-level token-budget enforcement. Over-budget facets drop off
   the tail of the combined sequence, not proportionally from each role,
   because a starved role is honestly worse than a lopsided bundle the
   caller can notice from ``per_role`` counts.
4. Optional ``explain`` mode emits a short ``reason`` field per facet so
   users iterating on SWCR parameters can inspect why a facet made the
   cut without rerunning the pipeline from a different harness.
5. Exactly one ``identity_bundle_assembled`` audit row per call, even
   on mid-assembly exception — same try/finally contract the retrieval
   pipeline uses so replay from the audit log never has gaps.

The v0.1 schema exposes only episodic/semantic/style facets, so only
the voice and recent_events roles are active by default. ``roles``
can still be supplied explicitly to exercise the future-role code
paths in tests; ``active_roles_for_schema`` drops roles whose
facet_type is not in the live vault's supported set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import sqlcipher3

from tessera.identity.roles import (
    BUNDLE_DEFAULT_BUDGET_TOKENS,
    BUNDLE_DEFAULT_WINDOW_HOURS,
    DEFAULT_ROLES,
    RoleSpec,
    active_roles_for_schema,
    normalise_budget_fractions,
)
from tessera.retrieval.pipeline import PipelineContext, RecallMatch, RecallResult, recall
from tessera.vault import audit

# Default query text per role. BM25 + dense hybrid within a single
# facet_type does not care deeply about the query wording because the
# facet_type filter does most of the work; the prompt seeds the dense
# embedding with role-flavoured signal.
_ROLE_QUERIES: dict[str, str] = {
    "voice": "style tone voice writing sample",
    "recent_events": "recent events decisions conversations actions",
    "skills": "learned procedures skills",
    "relationships": "people projects colleagues",
    "goals": "goals values priorities",
}


@dataclass(frozen=True, slots=True)
class IdentityFacet:
    external_id: str
    facet_type: str
    role: str
    snippet: str
    score: float
    captured_at: int
    token_count: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityBundle:
    facets: tuple[IdentityFacet, ...]
    per_role: dict[str, tuple[IdentityFacet, ...]]
    total_tokens: int
    total_budget_tokens: int
    truncated: bool
    seed: int
    stage_ms: dict[str, float]
    warnings: tuple[str, ...]
    rerank_degraded: bool


async def assume_identity(
    ctx: PipelineContext,
    *,
    model_hint: str | None = None,
    recent_window_hours: int = BUNDLE_DEFAULT_WINDOW_HOURS,
    total_budget_tokens: int = BUNDLE_DEFAULT_BUDGET_TOKENS,
    roles: tuple[RoleSpec, ...] = DEFAULT_ROLES,
    query_text: str | None = None,
    explain: bool = False,
    now_epoch: int | None = None,
) -> IdentityBundle:
    """Assemble and return an identity bundle for the active agent.

    ``query_text`` is optional; per-role defaults in ``_ROLE_QUERIES``
    supply the seed prompt when none is given. ``model_hint`` travels
    in the audit log (who is asking) but does not steer retrieval.
    """

    if total_budget_tokens <= 0:
        raise ValueError(f"total_budget_tokens must be positive; got {total_budget_tokens}")
    if recent_window_hours <= 0:
        raise ValueError(f"recent_window_hours must be positive; got {recent_window_hours}")

    now = now_epoch if now_epoch is not None else _now_epoch()
    active = active_roles_for_schema(roles)
    stage_ms: dict[str, float] = {}
    bundle_seed = _bundle_seed(
        model_hint=model_hint,
        recent_window_hours=recent_window_hours,
        total_budget_tokens=total_budget_tokens,
        query_text=query_text,
        roles=active,
        vault_id=ctx.vault_id,
    )
    per_role: dict[str, tuple[IdentityFacet, ...]] = {}
    combined_raw: list[IdentityFacet] = []
    rerank_degraded = False
    pipeline_error: str | None = None
    truncated = False
    total_tokens = 0
    warnings: list[str] = []

    try:
        if not active:
            raise ValueError(
                "no active roles for this vault schema; "
                "check that at least one role's facet_type is supported"
            )

        fractions = normalise_budget_fractions(active)

        t0 = time.perf_counter()
        per_role_tasks = [
            _run_role(
                ctx,
                role=role,
                query_text=query_text or _ROLE_QUERIES.get(role.name, role.name),
                sub_budget=_sub_budget(total_budget_tokens, fractions[role.name]),
            )
            for role in active
        ]
        role_results = await asyncio.gather(*per_role_tasks)
        stage_ms["roles"] = _elapsed_ms(t0)

        t0 = time.perf_counter()
        captured_at_by_ext = _fetch_captured_at(
            ctx.conn,
            (match.external_id for r in role_results for match in r.matches),
        )
        stage_ms["captured_at_lookup"] = _elapsed_ms(t0)

        for role, result in zip(active, role_results, strict=True):
            if result.rerank_degraded:
                rerank_degraded = True
            facets = _facets_for_role(
                role=role,
                matches=result.matches,
                captured_at_by_ext=captured_at_by_ext,
                now=now,
                explain=explain,
            )
            per_role[role.name] = facets
            combined_raw.extend(facets)

        t0 = time.perf_counter()
        kept, truncated = _apply_bundle_budget(combined_raw, budget=total_budget_tokens)
        stage_ms["budget"] = _elapsed_ms(t0)
        total_tokens = sum(f.token_count for f in kept)

        # Rebuild per_role to reflect budget trimming.
        per_role = _regroup_by_role(kept, active)
        combined_raw = kept

        # k_min warnings are recomputed against the FINAL per-role counts so
        # a role that met k_min pre-trim but fell below it after
        # bundle-level budget enforcement still surfaces to the caller. The
        # pre-trim check would miss exactly the case that matters —
        # silent starvation of a role under budget pressure.
        for role in active:
            count = len(per_role.get(role.name, ()))
            if count < role.k_min:
                warnings.append(f"role:{role.name} returned {count} < k_min={role.k_min}")
    except Exception as exc:
        pipeline_error = type(exc).__name__
        raise
    finally:
        _write_audit(
            ctx=ctx,
            seed=bundle_seed,
            model_hint=model_hint,
            recent_window_hours=recent_window_hours,
            total_budget_tokens=total_budget_tokens,
            total_tokens=total_tokens,
            per_role=per_role,
            truncated=truncated,
            stage_ms=stage_ms,
            rerank_degraded=rerank_degraded,
            pipeline_error=pipeline_error,
        )

    return IdentityBundle(
        facets=tuple(combined_raw),
        per_role={name: tuple(items) for name, items in per_role.items()},
        total_tokens=total_tokens,
        total_budget_tokens=total_budget_tokens,
        truncated=truncated,
        seed=bundle_seed,
        stage_ms=dict(stage_ms),
        warnings=tuple(warnings),
        rerank_degraded=rerank_degraded,
    )


async def _run_role(
    ctx: PipelineContext,
    *,
    role: RoleSpec,
    query_text: str,
    sub_budget: int,
) -> RecallResult:
    role_ctx = replace(
        ctx,
        facet_types=(role.facet_type,),
        k=role.k_max,
        tool_budget_tokens=sub_budget,
    )
    return await recall(role_ctx, query_text=query_text)


def _sub_budget(total_budget: int, fraction: float) -> int:
    # Floor division leaves a few tokens on the table; the bundle-level
    # cap picks them back up on the combined result.
    return max(1, int(total_budget * fraction))


def _fetch_captured_at(conn: sqlcipher3.Connection, external_ids: Iterable[str]) -> dict[str, int]:
    """Single IN-clause lookup for captured_at per facet.

    The P4 retrieval pipeline returns ``captured_at=0`` as a placeholder;
    the identity-bundle time-window filter needs the real column value.
    One query for the whole working set keeps the call O(1) rather than
    O(N) per-facet round-trips.
    """

    ids = [ext_id for ext_id in external_ids if ext_id]
    if not ids:
        return {}
    unique = list(dict.fromkeys(ids))
    placeholders = ",".join("?" for _ in unique)
    rows = conn.execute(
        f"SELECT external_id, captured_at FROM facets WHERE external_id IN ({placeholders})",
        tuple(unique),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _facets_for_role(
    *,
    role: RoleSpec,
    matches: Sequence[RecallMatch],
    captured_at_by_ext: dict[str, int],
    now: int,
    explain: bool,
) -> tuple[IdentityFacet, ...]:
    """Apply the role's time window and k_max cap; return typed facets."""

    window_cutoff = now - role.time_window_hours * 3600 if role.time_window_hours else None
    out: list[IdentityFacet] = []
    for match in matches:
        captured_at = captured_at_by_ext.get(match.external_id, 0)
        if window_cutoff is not None and captured_at < window_cutoff:
            continue
        reason: str | None = None
        if explain:
            reason = _reason_for(role=role, match=match, captured_at=captured_at, now=now)
        out.append(
            IdentityFacet(
                external_id=match.external_id,
                facet_type=match.facet_type,
                role=role.name,
                snippet=match.snippet,
                score=match.score,
                captured_at=captured_at,
                token_count=match.token_count,
                reason=reason,
            )
        )
        if len(out) >= role.k_max:
            break
    return tuple(out)


def _reason_for(*, role: RoleSpec, match: RecallMatch, captured_at: int, now: int) -> str:
    parts = [f"role={role.name}", f"rank={match.rank}", f"score={match.score:.3f}"]
    if role.time_window_hours is not None and captured_at > 0:
        age_hours = max(0, (now - captured_at) // 3600)
        parts.append(f"age={age_hours}h")
    return ",".join(parts)


def _apply_bundle_budget(
    facets: list[IdentityFacet], *, budget: int
) -> tuple[list[IdentityFacet], bool]:
    kept: list[IdentityFacet] = []
    used = 0
    for facet in facets:
        if used + facet.token_count > budget:
            return kept, True
        kept.append(facet)
        used += facet.token_count
    return kept, False


def _regroup_by_role(
    facets: list[IdentityFacet], active: tuple[RoleSpec, ...]
) -> dict[str, tuple[IdentityFacet, ...]]:
    # Preserve the role order from ``active`` so the bundle's iteration
    # shape is stable across calls with identical inputs.
    by_role: dict[str, list[IdentityFacet]] = {role.name: [] for role in active}
    for facet in facets:
        by_role[facet.role].append(facet)
    return {role.name: tuple(by_role[role.name]) for role in active}


def _bundle_seed(
    *,
    model_hint: str | None,
    recent_window_hours: int,
    total_budget_tokens: int,
    query_text: str | None,
    roles: tuple[RoleSpec, ...],
    vault_id: str,
) -> int:
    """Complete fingerprint of the caller's inputs.

    Folds in every parameter that can change which facets the bundle
    returns or how many survive truncation. Two calls that agree on all
    of these produce the same seed and — given a stable vault — the
    same bundle; a change to any one is a different identity regime.
    """

    payload = json.dumps(
        {
            "model_hint": model_hint or "",
            "window_hours": recent_window_hours,
            "total_budget_tokens": total_budget_tokens,
            "query_text": query_text or "",
            "vault_id": vault_id,
            "roles": [r.name for r in roles],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _write_audit(
    *,
    ctx: PipelineContext,
    seed: int,
    model_hint: str | None,
    recent_window_hours: int,
    total_budget_tokens: int,
    total_tokens: int,
    per_role: dict[str, tuple[IdentityFacet, ...]],
    truncated: bool,
    stage_ms: dict[str, float],
    rerank_degraded: bool,
    pipeline_error: str | None,
) -> None:
    payload: dict[str, Any] = {
        "seed": f"0x{seed:016x}",
        "model_hint": model_hint,
        "recent_window_hours": recent_window_hours,
        "retrieval_mode": ctx.config.retrieval_mode,
        "total_tokens": total_tokens,
        "total_budget_tokens": total_budget_tokens,
        "per_role_counts": {name: len(items) for name, items in per_role.items()},
        "truncated": truncated,
        "duration_ms": sum(stage_ms.values()),
        "rerank_degraded": rerank_degraded,
        "pipeline_error": pipeline_error,
    }
    try:
        audit.write(
            ctx.conn,
            op="identity_bundle_assembled",
            actor="identity",
            agent_id=ctx.agent_id,
            payload=payload,
        )
    except Exception as audit_exc:
        sys.stderr.write(
            f"identity_bundle_assembled audit write failed: {type(audit_exc).__name__}\n"
        )


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())
