"""MCP tool surface: capture, recall, assume_identity, show, list_facets, stats.

Each tool is a thin wrapper around a storage/retrieval primitive plus
four cross-cutting concerns the boundary owns and the primitives
below it do not:

1. Input validation per docs/security-standards.md — type/length/format
   /range checked at entry before any storage call.
2. Scope enforcement per ADR 0007 — read/write grants consulted against
   the verified capability before the primitive runs; denials land in
   ``scope_denied`` audit rows and raise :class:`ScopeDenied`.
3. Per-tool token-budget declaration — every response passes through
   ``apply_budget`` so no tool can return more tokens than it promised.
4. Audit emission — capture/recall/assume_identity delegate to their
   primitive's existing audit path; show/list_facets/stats are pure
   reads so they do not add audit entries.

The surface is intentionally storage-layer-thin. Each tool is a dozen
lines of validation + scope check + delegate + response-shape — the
heavy lifting lives in the retrieval pipeline (P4/P5), the identity
bundle assembler (P6), and the vault CRUD helpers (P3). Keeping the
boundary thin keeps the audit and scope invariants legible.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import sqlcipher3

from tessera.adapters import models_registry
from tessera.auth.scopes import Scope, ScopeOp
from tessera.auth.tokens import VerifiedCapability, record_scope_denial
from tessera.identity.bundle import (
    IdentityBundle,
)
from tessera.identity.bundle import (
    assume_identity as _assume_identity,
)
from tessera.identity.roles import BUNDLE_DEFAULT_WINDOW_HOURS
from tessera.retrieval.budget import BudgetedItem, apply_budget, count_tokens, truncate_snippet
from tessera.retrieval.pipeline import PipelineContext, RecallResult
from tessera.retrieval.pipeline import recall as _pipeline_recall
from tessera.vault import capture as vault_capture
from tessera.vault import facets as vault_facets

# Input validation limits. These are hard caps; any payload exceeding
# them is rejected at the boundary with :class:`ValidationError` rather
# than being silently truncated — truncation would hide an adversarial
# or buggy caller from the operator.
_MAX_CONTENT_CHARS: Final[int] = 65_536
_MAX_QUERY_CHARS: Final[int] = 4_096
_MAX_MODEL_HINT_CHARS: Final[int] = 128
_MIN_K: Final[int] = 1
_MAX_K: Final[int] = 100
_MIN_LIMIT: Final[int] = 1
_MAX_LIMIT: Final[int] = 100
# Recent-window cap at one year in hours — long enough for any sensible
# identity-bundle horizon, short enough to bound the SQL scan.
_MIN_WINDOW_HOURS: Final[int] = 1
_MAX_WINDOW_HOURS: Final[int] = 8_760
# Metadata is a small structured blob, not a freeform dump. Caps here
# mirror the "validate all input at the system boundary" rule for
# dict-shaped payloads: a bounded JSON size and a shallow depth.
_MAX_METADATA_KEYS: Final[int] = 32
_MAX_METADATA_BYTES: Final[int] = 4_096
# ``since`` is a Unix epoch in seconds. Reject negatives and anything
# past year 9999 so a malformed int cannot drive the SQL WHERE to an
# unreachable partition.
_MIN_SINCE_EPOCH: Final[int] = 0
_MAX_SINCE_EPOCH: Final[int] = 253_402_300_799  # 9999-12-31T23:59:59Z

# Per-tool token budgets (cl100k_base). Callers may request smaller
# budgets but never larger — ``recall`` and ``assume_identity`` clamp
# their requested_budget to the declared ceiling.
CAPTURE_RESPONSE_BUDGET: Final[int] = 512
RECALL_RESPONSE_BUDGET: Final[int] = 6_000
ASSUME_IDENTITY_RESPONSE_BUDGET: Final[int] = 6_000
SHOW_RESPONSE_BUDGET: Final[int] = 2_048
LIST_FACETS_RESPONSE_BUDGET: Final[int] = 2_048
STATS_RESPONSE_BUDGET: Final[int] = 1_024

# ULID shape: 26 chars Crockford base32. We accept the canonical upper
# alphabet only; the facets module mints via python-ulid which emits
# uppercase, and allowing lowercase would double the enumeration
# surface a stolen vault's ``show`` calls have to defend against.
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_CLIENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ToolError(Exception):
    """Base class for MCP-boundary errors.

    Every subclass carries a stable ``code`` attribute so the future
    JSON-RPC wire format can return discriminated error payloads
    without leaking internal exception types.
    """

    code: str = "tool_error"


class ValidationError(ToolError):
    """Input failed a boundary-level validator (length/format/range)."""

    code = "invalid_input"


class ScopeDenied(ToolError):
    """Capability did not carry the scope required for this tool call."""

    code = "scope_denied"

    def __init__(self, required_op: ScopeOp, facet_type: str) -> None:
        super().__init__(f"scope required: {required_op} on {facet_type}")
        self.required_op = required_op
        self.required_facet_type = facet_type


class BudgetExceeded(ToolError):
    """Response exceeded the tool's declared token budget.

    Raised when the pre-budget response would exceed the declared
    ceiling and the caller supplied an adversarial input that cannot be
    safely trimmed (e.g., show() on a facet whose snippet alone exceeds
    the budget after per-snippet truncation). Normal overflow trims
    trailing items and returns truncated=True.
    """

    code = "budget_exceeded"


class StorageError(ToolError):
    """A storage-layer primitive raised; wrapped so the error-code
    contract holds at the MCP boundary.

    Used for downstream exceptions that escape the ``ToolError``
    hierarchy (unknown agent id, unsupported facet type after a race,
    vault-level IO errors). The wire layer renders a stable
    ``storage_error`` code and does not leak the underlying exception
    class or message verbatim.
    """

    code = "storage_error"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything an MCP tool needs that was resolved before dispatch.

    ``pipeline`` is optional so tools that do not touch retrieval
    (capture, show, list_facets, stats) can be called without wiring an
    embedder or reranker. Tools that do require retrieval raise
    :class:`ValidationError` when ``pipeline`` is missing.
    """

    conn: sqlcipher3.Connection
    verified: VerifiedCapability
    vault_path: Path
    clock: Callable[[], int] = field(default_factory=lambda: _now_epoch)
    pipeline: PipelineContext | None = None


# ---- Response dataclasses -----------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    external_id: str
    is_duplicate: bool
    facet_type: str


@dataclass(frozen=True, slots=True)
class RecallMatchView:
    external_id: str
    facet_type: str
    snippet: str
    score: float
    rank: int
    captured_at: int
    token_count: int


@dataclass(frozen=True, slots=True)
class RecallResponse:
    matches: tuple[RecallMatchView, ...]
    warnings: tuple[str, ...]
    seed: int
    truncated: bool
    rerank_degraded: bool
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AssumeIdentityResponse:
    facets: tuple[RecallMatchView, ...]
    per_role_counts: dict[str, int]
    total_tokens: int
    total_budget_tokens: int
    truncated: bool
    warnings: tuple[str, ...]
    seed: int


@dataclass(frozen=True, slots=True)
class ShowResponse:
    external_id: str
    facet_type: str
    snippet: str
    captured_at: int
    source_client: str
    embed_status: str
    token_count: int


@dataclass(frozen=True, slots=True)
class FacetSummary:
    external_id: str
    facet_type: str
    snippet: str
    captured_at: int
    source_client: str
    embed_status: str


@dataclass(frozen=True, slots=True)
class ListFacetsResponse:
    items: tuple[FacetSummary, ...]
    truncated: bool
    total_tokens: int


@dataclass(frozen=True, slots=True)
class EmbedHealth:
    pending: int
    embedded: int
    failed: int
    stale: int


@dataclass(frozen=True, slots=True)
class ActiveModel:
    name: str
    dim: int


@dataclass(frozen=True, slots=True)
class StatsResponse:
    embed_health: EmbedHealth
    by_source: dict[str, int]
    active_models: tuple[ActiveModel, ...]
    vault_size_bytes: int
    facet_count: int


# ---- Tools ---------------------------------------------------------------


async def capture(
    tctx: ToolContext,
    *,
    content: str,
    facet_type: str,
    source_client: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CaptureResponse:
    """MCP ``capture`` — insert a facet.

    Delegates to :func:`tessera.vault.capture.capture` after write-scope
    enforcement. ``source_client`` defaults to the capability's
    ``client_name`` when omitted; callers may override to attribute a
    capture to a specific sub-agent, but the capability's client_name
    is what lands in the audit row regardless.
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    _validate_facet_type(facet_type)
    _validate_metadata(metadata)
    resolved_source = source_client or tctx.verified.client_name
    _validate_client_name(resolved_source)
    _require_scope(tctx, op="write", facet_type=facet_type)
    try:
        result = vault_capture.capture(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            facet_type=facet_type,
            content=content,
            source_client=resolved_source,
            metadata=metadata,
        )
    except vault_facets.UnknownAgentError as exc:
        # Agent rows are the vault's stable root; a capability pointing
        # at a vanished agent is a data-integrity break that the MCP
        # boundary surfaces as a storage error with a stable code.
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return CaptureResponse(
        external_id=result.external_id,
        is_duplicate=result.is_duplicate,
        facet_type=facet_type,
    )


async def recall(
    tctx: ToolContext,
    *,
    query_text: str,
    k: int,
    facet_types: Sequence[str] | None = None,
    requested_budget_tokens: int | None = None,
) -> RecallResponse:
    """MCP ``recall`` — hybrid retrieval + rerank + budget.

    ``facet_types`` defaults to the pipeline's configured set, which in
    the v0.1 schema is (style, episodic). Read-scope is checked per
    requested facet type — a partial scope denial raises rather than
    returning a filtered subset, so the caller cannot accidentally
    observe a narrower result than they asked for.
    """

    if tctx.pipeline is None:
        raise ValidationError("recall requires a configured pipeline context")
    _validate_length("query_text", query_text, _MAX_QUERY_CHARS, allow_empty=False)
    _validate_k(k)
    types = tuple(facet_types) if facet_types is not None else tctx.pipeline.facet_types
    for t in types:
        _validate_facet_type(t)
        _require_scope(tctx, op="read", facet_type=t)
    budget_tokens = _resolve_response_budget(requested_budget_tokens, RECALL_RESPONSE_BUDGET)
    result = await _pipeline_recall(
        _replace_pipeline(tctx.pipeline, k=k, facet_types=types, tool_budget=budget_tokens),
        query_text=query_text,
    )
    matches = _shape_recall_matches(result)
    trimmed, truncated = _enforce_response_budget(matches, budget_tokens)
    return RecallResponse(
        matches=trimmed,
        warnings=result.warnings,
        seed=result.seed,
        truncated=truncated or result.truncated,
        rerank_degraded=result.rerank_degraded,
        total_tokens=sum(m.token_count for m in trimmed),
    )


async def assume_identity(
    tctx: ToolContext,
    *,
    model_hint: str | None = None,
    recent_window_hours: int = BUNDLE_DEFAULT_WINDOW_HOURS,
    requested_budget_tokens: int | None = None,
    query_text: str | None = None,
    explain: bool = False,
) -> AssumeIdentityResponse:
    """MCP ``assume_identity`` — role-diversified identity bundle.

    Read-scope is required on every role's ``facet_type``. The identity
    module enforces its own budget internally; this wrapper re-runs the
    budget on the flattened response as a belt-and-braces check so a
    misconfigured role could not exceed the declared ceiling.
    """

    if tctx.pipeline is None:
        raise ValidationError("assume_identity requires a configured pipeline context")
    if model_hint is not None:
        _validate_length("model_hint", model_hint, _MAX_MODEL_HINT_CHARS, allow_empty=False)
    if query_text is not None:
        _validate_length("query_text", query_text, _MAX_QUERY_CHARS, allow_empty=False)
    _validate_recent_window_hours(recent_window_hours)
    budget_tokens = _resolve_response_budget(
        requested_budget_tokens, ASSUME_IDENTITY_RESPONSE_BUDGET
    )
    # Scope check: every active role's facet_type must be readable. We
    # look up the role list the assembler would use so a caller cannot
    # slip past the scope gate by the assembler silently dropping roles.
    from tessera.identity.roles import active_roles_for_schema  # avoid import cycle

    for role in active_roles_for_schema():
        _require_scope(tctx, op="read", facet_type=role.facet_type)
    bundle: IdentityBundle = await _assume_identity(
        tctx.pipeline,
        model_hint=model_hint,
        recent_window_hours=recent_window_hours,
        total_budget_tokens=budget_tokens,
        query_text=query_text,
        explain=explain,
    )
    matches = tuple(
        RecallMatchView(
            external_id=f.external_id,
            facet_type=f.facet_type,
            snippet=f.snippet,
            score=f.score,
            rank=i,
            captured_at=f.captured_at,
            token_count=f.token_count,
        )
        for i, f in enumerate(bundle.facets)
    )
    trimmed, truncated = _enforce_response_budget(matches, budget_tokens)
    per_role_counts = {role: len(items) for role, items in bundle.per_role.items()}
    return AssumeIdentityResponse(
        facets=trimmed,
        per_role_counts=per_role_counts,
        total_tokens=sum(m.token_count for m in trimmed),
        total_budget_tokens=bundle.total_budget_tokens,
        truncated=truncated or bundle.truncated,
        warnings=bundle.warnings,
        seed=bundle.seed,
    )


async def show(tctx: ToolContext, *, external_id: str) -> ShowResponse:
    """MCP ``show`` — fetch one facet by ULID with read-scope."""

    _validate_ulid(external_id)
    facet = vault_facets.get(tctx.conn, external_id)
    if facet is None or facet.is_deleted:
        raise ValidationError(f"facet {external_id!r} does not exist")
    _require_scope(tctx, op="read", facet_type=facet.facet_type)
    snippet = truncate_snippet(facet.content)
    token_count = count_tokens(snippet)
    if token_count > SHOW_RESPONSE_BUDGET:
        # Post-snippet-truncation overflow — per-snippet truncation
        # failed to bring us under the budget. Refuse rather than send
        # over-budget data.
        raise BudgetExceeded(
            f"show response token_count={token_count} exceeds budget={SHOW_RESPONSE_BUDGET}"
        )
    return ShowResponse(
        external_id=facet.external_id,
        facet_type=facet.facet_type,
        snippet=snippet,
        captured_at=facet.captured_at,
        source_client=facet.source_client,
        embed_status=facet.embed_status,
        token_count=token_count,
    )


async def list_facets(
    tctx: ToolContext,
    *,
    facet_type: str,
    limit: int = 20,
    since: int | None = None,
) -> ListFacetsResponse:
    """MCP ``list_facets`` — paginated metadata of a facet type."""

    _validate_facet_type(facet_type)
    _validate_limit(limit)
    if since is not None:
        _validate_since(since)
    _require_scope(tctx, op="read", facet_type=facet_type)
    rows = vault_facets.list_by_type(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        facet_type=facet_type,
        limit=limit,
        since=since,
    )
    summaries = [
        FacetSummary(
            external_id=r.external_id,
            facet_type=r.facet_type,
            snippet=truncate_snippet(r.content, max_tokens=64),
            captured_at=r.captured_at,
            source_client=r.source_client,
            embed_status=r.embed_status,
        )
        for r in rows
    ]
    budget = _as_budgeted(summaries)
    trimmed = apply_budget(budget, total_budget=LIST_FACETS_RESPONSE_BUDGET)
    kept_keys = {item.key for item in trimmed.items}
    items = tuple(s for s in summaries if s.external_id in kept_keys)
    total_tokens = sum(item.token_count for item in trimmed.items)
    return ListFacetsResponse(
        items=items,
        truncated=trimmed.truncated,
        total_tokens=total_tokens,
    )


async def stats(tctx: ToolContext) -> StatsResponse:
    """MCP ``stats`` — vault health and provenance snapshot.

    Returns embed health, per-source counts, active embedding models,
    and on-disk vault size. No scope check beyond having a valid
    capability: the payload is counts only, no content.
    """

    embed_health = _embed_health(tctx.conn, agent_id=tctx.verified.agent_id)
    by_source = _counts_by_source(tctx.conn, agent_id=tctx.verified.agent_id)
    active_models = tuple(
        ActiveModel(name=m.name, dim=m.dim)
        for m in models_registry.list_models(tctx.conn)
        if m.is_active
    )
    vault_size = tctx.vault_path.stat().st_size if tctx.vault_path.exists() else 0
    facet_count = int(
        tctx.conn.execute(
            "SELECT COUNT(*) FROM facets WHERE agent_id = ? AND is_deleted = 0",
            (tctx.verified.agent_id,),
        ).fetchone()[0]
    )
    return StatsResponse(
        embed_health=embed_health,
        by_source=by_source,
        active_models=active_models,
        vault_size_bytes=vault_size,
        facet_count=facet_count,
    )


# ---- Helpers -------------------------------------------------------------


def _require_scope(tctx: ToolContext, *, op: ScopeOp, facet_type: str) -> None:
    scope: Scope = tctx.verified.scope
    if scope.allows(op=op, facet_type=facet_type):
        return
    record_scope_denial(
        tctx.conn,
        token_id=tctx.verified.token_id,
        client_name=tctx.verified.client_name,
        required_op=op,
        required_facet_type=facet_type,
        now_epoch=tctx.clock(),
    )
    raise ScopeDenied(op, facet_type)


def _validate_length(field_name: str, value: str, max_chars: int, *, allow_empty: bool) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > max_chars:
        raise ValidationError(f"{field_name} length {len(value)} exceeds max {max_chars}")


def _validate_facet_type(facet_type: str) -> None:
    if facet_type not in vault_facets.V0_1_FACET_TYPES:
        raise ValidationError(
            f"facet_type {facet_type!r} not in {sorted(vault_facets.V0_1_FACET_TYPES)}"
        )


def _validate_client_name(client_name: str) -> None:
    if not _CLIENT_NAME_PATTERN.match(client_name):
        raise ValidationError(
            f"client_name {client_name!r} must match {_CLIENT_NAME_PATTERN.pattern}"
        )


def _validate_k(k: int) -> None:
    if not isinstance(k, int) or isinstance(k, bool):
        raise ValidationError("k must be an integer")
    if k < _MIN_K or k > _MAX_K:
        raise ValidationError(f"k={k} outside [{_MIN_K}, {_MAX_K}]")


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValidationError("limit must be an integer")
    if limit < _MIN_LIMIT or limit > _MAX_LIMIT:
        raise ValidationError(f"limit={limit} outside [{_MIN_LIMIT}, {_MAX_LIMIT}]")


def _validate_recent_window_hours(hours: int) -> None:
    if not isinstance(hours, int) or isinstance(hours, bool):
        raise ValidationError("recent_window_hours must be an integer")
    if hours < _MIN_WINDOW_HOURS or hours > _MAX_WINDOW_HOURS:
        raise ValidationError(
            f"recent_window_hours={hours} outside [{_MIN_WINDOW_HOURS}, {_MAX_WINDOW_HOURS}]"
        )


def _validate_since(since: int) -> None:
    if not isinstance(since, int) or isinstance(since, bool):
        raise ValidationError("since must be an integer")
    if since < _MIN_SINCE_EPOCH or since > _MAX_SINCE_EPOCH:
        raise ValidationError(f"since={since} outside [{_MIN_SINCE_EPOCH}, {_MAX_SINCE_EPOCH}]")


def _validate_metadata(metadata: dict[str, Any] | None) -> None:
    """Bound metadata shape before it reaches storage.

    Enforces a top-level-key ceiling and a serialised-byte ceiling. Key
    types are checked because SQLite's JSON treatment of non-string
    keys is not the caller's business to rely on.
    """

    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise ValidationError(f"metadata must be a dict, got {type(metadata).__name__}")
    if len(metadata) > _MAX_METADATA_KEYS:
        raise ValidationError(f"metadata has {len(metadata)} keys; max {_MAX_METADATA_KEYS}")
    for key in metadata:
        if not isinstance(key, str):
            raise ValidationError(f"metadata keys must be strings, got {type(key).__name__}")
    serialised = json.dumps(metadata, ensure_ascii=False)
    if len(serialised.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValidationError(f"metadata serialised size exceeds {_MAX_METADATA_BYTES} bytes")


def _validate_ulid(value: str) -> None:
    if not isinstance(value, str) or not _ULID_PATTERN.match(value):
        raise ValidationError(f"external_id {value!r} is not a valid ULID")


def _resolve_response_budget(requested: int | None, ceiling: int) -> int:
    if requested is None:
        return ceiling
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise ValidationError("requested_budget_tokens must be an integer")
    if requested < 1:
        raise ValidationError(f"requested_budget_tokens={requested} must be positive")
    return min(requested, ceiling)


def _replace_pipeline(
    ctx: PipelineContext,
    *,
    k: int,
    facet_types: tuple[str, ...],
    tool_budget: int,
) -> PipelineContext:
    from dataclasses import replace

    return replace(ctx, k=k, facet_types=facet_types, tool_budget_tokens=tool_budget)


def _shape_recall_matches(result: RecallResult) -> tuple[RecallMatchView, ...]:
    return tuple(
        RecallMatchView(
            external_id=m.external_id,
            facet_type=m.facet_type,
            snippet=m.snippet,
            score=m.score,
            rank=m.rank,
            captured_at=m.captured_at,
            token_count=m.token_count,
        )
        for m in result.matches
    )


def _enforce_response_budget(
    matches: tuple[RecallMatchView, ...], budget_tokens: int
) -> tuple[tuple[RecallMatchView, ...], bool]:
    used = 0
    kept: list[RecallMatchView] = []
    truncated = False
    for m in matches:
        if used + m.token_count > budget_tokens:
            truncated = True
            break
        kept.append(m)
        used += m.token_count
    if len(kept) < len(matches):
        truncated = True
    return tuple(kept), truncated


def _as_budgeted(summaries: list[FacetSummary]) -> list[BudgetedItem]:
    items = []
    for s in summaries:
        # Budget a summary by its snippet + a fixed per-row overhead for
        # the other metadata fields; conservative so the response never
        # squeezes past the declared ceiling even with generous ULIDs.
        snippet_tokens = count_tokens(s.snippet)
        per_row_overhead = 32
        items.append(
            BudgetedItem(
                key=s.external_id,
                snippet=s.snippet,
                token_count=snippet_tokens + per_row_overhead,
            )
        )
    return items


def _embed_health(conn: sqlcipher3.Connection, *, agent_id: int) -> EmbedHealth:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN embed_status = 'pending'  THEN 1 ELSE 0 END),
            SUM(CASE WHEN embed_status = 'embedded' THEN 1 ELSE 0 END),
            SUM(CASE WHEN embed_status = 'failed'   THEN 1 ELSE 0 END),
            SUM(CASE WHEN embed_status = 'stale'    THEN 1 ELSE 0 END)
        FROM facets
        WHERE agent_id = ? AND is_deleted = 0
        """,
        (agent_id,),
    ).fetchone()
    pending, embedded, failed, stale = (int(v) if v is not None else 0 for v in row)
    return EmbedHealth(pending=pending, embedded=embedded, failed=failed, stale=stale)


def _counts_by_source(conn: sqlcipher3.Connection, *, agent_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT source_client, COUNT(*)
          FROM facets
         WHERE agent_id = ? AND is_deleted = 0
      GROUP BY source_client
      ORDER BY source_client
        """,
        (agent_id,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


__all__ = [
    "ASSUME_IDENTITY_RESPONSE_BUDGET",
    "CAPTURE_RESPONSE_BUDGET",
    "LIST_FACETS_RESPONSE_BUDGET",
    "RECALL_RESPONSE_BUDGET",
    "SHOW_RESPONSE_BUDGET",
    "STATS_RESPONSE_BUDGET",
    "ActiveModel",
    "AssumeIdentityResponse",
    "BudgetExceeded",
    "CaptureResponse",
    "EmbedHealth",
    "FacetSummary",
    "ListFacetsResponse",
    "RecallMatchView",
    "RecallResponse",
    "ScopeDenied",
    "ShowResponse",
    "StatsResponse",
    "ToolContext",
    "ToolError",
    "ValidationError",
    "assume_identity",
    "capture",
    "list_facets",
    "recall",
    "show",
    "stats",
]
