"""Method-name → MCP tool dispatcher.

Translates an HTTP MCP request's ``method`` string into a call on the
matching :mod:`tessera.mcp_surface.tools` function. The translator
lives here rather than inside ``tools.py`` so the tool surface stays
HTTP-transport agnostic — P14's stdio bridge reuses the same tools via
a sibling dispatcher with no changes to the tool module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tessera.auth.tokens import VerifiedCapability
from tessera.daemon.state import DaemonState, build_pipeline_context
from tessera.mcp_surface import tools as mcp
from tessera.vault import facets as vault_facets


class UnknownMethodError(Exception):
    """Request carried a method name outside the v0.1 tool surface."""


async def dispatch_tool_call(
    state: DaemonState,
    verified: VerifiedCapability,
    method: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Route ``method`` to its tool and return a JSON-serialisable result.

    Errors from the tool layer propagate unchanged — the HTTP wrapper
    catches them and maps to HTTP status codes; the control-plane
    wrapper maps them to ``ok=False`` envelopes.
    """

    handler = _HANDLERS.get(method)
    if handler is None:
        raise UnknownMethodError(f"unknown method {method!r}")
    tctx = _tool_context(state, verified)
    return await handler(tctx, args)


def _tool_context(state: DaemonState, verified: VerifiedCapability) -> mcp.ToolContext:
    # Default ``facet_types`` is every writable type the token can read.
    # This is the cross-facet default the reframe requires: a recall()
    # without an explicit filter assembles a bundle across every facet
    # the caller is scoped for, which is what makes the T-shape
    # synthesis story work (``docs/system-design.md §Retrieval
    # pipeline``). v0.3 adds ``skill`` to the default fan-out so user
    # procedures surface alongside identity / preference / workflow /
    # project / style without requiring an explicit ``facet_types``
    # argument. ``person`` is *not* in the default because people are
    # not facets — they live in the ``people`` table and surface via
    # the ``resolve_person`` MCP tool, not ``recall``.
    scoped_types = tuple(
        ftype
        for ftype in _DEFAULT_RECALL_TYPES
        if verified.scope.allows(op="read", facet_type=ftype)
    )
    return mcp.ToolContext(
        conn=state.vault.connection,
        verified=verified,
        vault_path=state.vault_path,
        pipeline=build_pipeline_context(
            state,
            agent_id=verified.agent_id,
            tool_budget_tokens=mcp.RECALL_RESPONSE_BUDGET,
            k=20,
            facet_types=scoped_types,
        ),
        event_log=state.event_log,
    )


# Facet types a recall() without an explicit ``facet_types`` filter fans
# out over — every facet type the v0.3 write path can produce, sorted
# deterministically so scope-filtered subsets still produce a stable
# order in the pipeline context. People are excluded (they are not
# facets); compiled_notebook is excluded (deferred to v0.5).
_DEFAULT_RECALL_TYPES: tuple[str, ...] = tuple(
    sorted(vault_facets.WRITABLE_FACET_TYPES - {"person"})
)


async def _do_capture(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.capture(
        tctx,
        content=_require_str(args, "content"),
        facet_type=_require_str(args, "facet_type"),
        source_tool=args.get("source_tool"),
        metadata=args.get("metadata"),
        volatility=args.get("volatility", "persistent"),
        ttl_seconds=args.get("ttl_seconds"),
    )
    return {
        "external_id": resp.external_id,
        "is_duplicate": resp.is_duplicate,
        "facet_type": resp.facet_type,
        "volatility": resp.volatility,
        "ttl_seconds": resp.ttl_seconds,
    }


async def _do_recall(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    facet_types = args.get("facet_types")
    resp = await mcp.recall(
        tctx,
        query_text=_require_str(args, "query_text"),
        k=_optional_int(args, "k", default=10),
        facet_types=tuple(facet_types) if isinstance(facet_types, list) else None,
        requested_budget_tokens=args.get("requested_budget_tokens"),
    )
    return {
        "matches": [_match_to_json(m) for m in resp.matches],
        "warnings": list(resp.warnings),
        "degraded_reason": str(resp.degraded_reason) if resp.degraded_reason is not None else None,
        "seed": resp.seed,
        "truncated": resp.truncated,
        "rerank_degraded": resp.rerank_degraded,
        "total_tokens": resp.total_tokens,
    }


async def _do_show(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.show(tctx, external_id=_require_str(args, "external_id"))
    return {
        "external_id": resp.external_id,
        "facet_type": resp.facet_type,
        "snippet": resp.snippet,
        "captured_at": resp.captured_at,
        "source_tool": resp.source_tool,
        "embed_status": resp.embed_status,
        "token_count": resp.token_count,
    }


async def _do_forget(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.forget(
        tctx,
        external_id=_require_str(args, "external_id"),
        reason=args.get("reason"),
    )
    return {
        "external_id": resp.external_id,
        "facet_type": resp.facet_type,
        "deleted_at": resp.deleted_at,
    }


async def _do_list_facets(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.list_facets(
        tctx,
        facet_type=_require_str(args, "facet_type"),
        limit=int(args.get("limit", 20)),
        since=args.get("since"),
    )
    return {
        "items": [_summary_to_json(s) for s in resp.items],
        "truncated": resp.truncated,
        "total_tokens": resp.total_tokens,
    }


async def _do_stats(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    del args
    resp = await mcp.stats(tctx)
    return {
        "embed_health": {
            "pending": resp.embed_health.pending,
            "embedded": resp.embed_health.embedded,
            "failed": resp.embed_health.failed,
            "stale": resp.embed_health.stale,
        },
        "by_source": resp.by_source,
        "active_models": [{"name": m.name, "dim": m.dim} for m in resp.active_models],
        "vault_size_bytes": resp.vault_size_bytes,
        "facet_count": resp.facet_count,
    }


async def _do_learn_skill(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.learn_skill(
        tctx,
        name=_require_str(args, "name"),
        description=_require_str(args, "description"),
        procedure_md=_require_str(args, "procedure_md"),
        source_tool=args.get("source_tool"),
    )
    return {
        "external_id": resp.external_id,
        "name": resp.name,
        "is_new": resp.is_new,
    }


async def _do_get_skill(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.get_skill(tctx, name=_require_str(args, "name"))
    if resp is None:
        return {"skill": None}
    return {
        "skill": {
            "external_id": resp.external_id,
            "name": resp.name,
            "description": resp.description,
            "procedure_md": resp.procedure_md,
            "active": resp.active,
            "disk_path": resp.disk_path,
            "captured_at": resp.captured_at,
            "embed_status": resp.embed_status,
            "truncated": resp.truncated,
            "token_count": resp.token_count,
        }
    }


async def _do_list_skills(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    active_only = args.get("active_only", True)
    if not isinstance(active_only, bool):
        raise mcp.ValidationError("active_only must be a boolean")
    resp = await mcp.list_skills(
        tctx,
        active_only=active_only,
        limit=_optional_int(args, "limit", default=50),
    )
    return {
        "items": [
            {
                "external_id": s.external_id,
                "name": s.name,
                "description": s.description,
                "active": s.active,
                "captured_at": s.captured_at,
            }
            for s in resp.items
        ],
        "truncated": resp.truncated,
        "total_tokens": resp.total_tokens,
    }


async def _do_resolve_person(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.resolve_person(tctx, mention=_require_str(args, "mention"))
    return {
        "matches": [_person_to_json(m) for m in resp.matches],
        "is_exact": resp.is_exact,
    }


async def _do_list_people(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.list_people(
        tctx,
        limit=_optional_int(args, "limit", default=50),
        since=args.get("since"),
    )
    return {
        "items": [_person_to_json(m) for m in resp.items],
        "truncated": resp.truncated,
        "total_tokens": resp.total_tokens,
    }


_HandlerT = Callable[[mcp.ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]

_HANDLERS: dict[str, _HandlerT] = {
    "capture": _do_capture,
    "recall": _do_recall,
    "show": _do_show,
    "list_facets": _do_list_facets,
    "stats": _do_stats,
    "forget": _do_forget,
    "learn_skill": _do_learn_skill,
    "get_skill": _do_get_skill,
    "list_skills": _do_list_skills,
    "resolve_person": _do_resolve_person,
    "list_people": _do_list_people,
}


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise mcp.ValidationError(f"{key} must be a string")
    return value


def _require_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise mcp.ValidationError(f"{key} must be an integer")
    return value


def _optional_int(args: dict[str, Any], key: str, *, default: int) -> int:
    if key not in args:
        return default
    return _require_int(args, key)


def _match_to_json(m: mcp.RecallMatchView) -> dict[str, Any]:
    return {
        "external_id": m.external_id,
        "facet_type": m.facet_type,
        "snippet": m.snippet,
        "score": m.score,
        "rank": m.rank,
        "captured_at": m.captured_at,
        "token_count": m.token_count,
    }


def _summary_to_json(s: mcp.FacetSummary) -> dict[str, Any]:
    return {
        "external_id": s.external_id,
        "facet_type": s.facet_type,
        "snippet": s.snippet,
        "captured_at": s.captured_at,
        "source_tool": s.source_tool,
        "embed_status": s.embed_status,
    }


def _person_to_json(p: mcp.PersonMatch) -> dict[str, Any]:
    return {
        "external_id": p.external_id,
        "canonical_name": p.canonical_name,
        "aliases": list(p.aliases),
        "created_at": p.created_at,
    }


__all__ = [
    "UnknownMethodError",
    "dispatch_tool_call",
]
