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
    # Default ``facet_types`` is every v0.1 type the token can read. This is
    # the cross-facet default the reframe requires: a recall() without an
    # explicit filter assembles a bundle across every facet the caller is
    # scoped for, which is what makes the T-shape synthesis story work
    # (``docs/system-design.md §Retrieval pipeline``). A caller that wants a
    # single-facet-type recall passes ``facet_types=[...]`` explicitly.
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
    )


# The v0.1 facet types a recall() without an explicit ``facet_types`` filter
# fans out over — sorted deterministically so scope-filtered subsets still
# produce a stable order in the pipeline context.
_DEFAULT_RECALL_TYPES: tuple[str, ...] = tuple(sorted(vault_facets.V0_1_FACET_TYPES))


async def _do_capture(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    resp = await mcp.capture(
        tctx,
        content=_require_str(args, "content"),
        facet_type=_require_str(args, "facet_type"),
        source_tool=args.get("source_tool"),
        metadata=args.get("metadata"),
    )
    return {
        "external_id": resp.external_id,
        "is_duplicate": resp.is_duplicate,
        "facet_type": resp.facet_type,
    }


async def _do_recall(tctx: mcp.ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    facet_types = args.get("facet_types")
    resp = await mcp.recall(
        tctx,
        query_text=_require_str(args, "query_text"),
        k=_require_int(args, "k"),
        facet_types=tuple(facet_types) if isinstance(facet_types, list) else None,
        requested_budget_tokens=args.get("requested_budget_tokens"),
    )
    return {
        "matches": [_match_to_json(m) for m in resp.matches],
        "warnings": list(resp.warnings),
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


_HandlerT = Callable[[mcp.ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]

_HANDLERS: dict[str, _HandlerT] = {
    "capture": _do_capture,
    "recall": _do_recall,
    "show": _do_show,
    "list_facets": _do_list_facets,
    "stats": _do_stats,
    "forget": _do_forget,
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


__all__ = [
    "UnknownMethodError",
    "dispatch_tool_call",
]
