"""MCP tool surface: capture, recall, show, list_facets, stats, forget,
plus the v0.3 People + Skills tools (learn_skill, get_skill, list_skills,
resolve_person, list_people).

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
4. Audit emission — capture/recall/forget/learn_skill delegate to their
   primitive's existing audit path; show/list_facets/stats/list_skills/
   list_people/resolve_person/get_skill are pure reads and do not add
   audit entries.

The surface is intentionally storage-layer-thin. Each tool is a dozen
lines of validation + scope check + delegate + response-shape — the
heavy lifting lives in the retrieval pipeline and the vault CRUD
helpers. Keeping the boundary thin keeps the audit and scope
invariants legible.

The sixth core tool is ``forget`` (soft-delete with an audit entry).
Cross-facet context is delivered by ``recall`` with ``facet_types``
defaulting to every type the caller is scoped for (per ADR 0010). The
v0.3 surface adds five tools that operate on the People + Skills
side: ``learn_skill`` writes a skill (write scope on ``skill``),
``get_skill`` and ``list_skills`` read skills (read scope on
``skill``), ``resolve_person`` turns a free-form mention into a
candidate ``Person`` list, and ``list_people`` enumerates the agent's
people roster (both read scope on ``person``).
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
from tessera.observability.events import EventLog
from tessera.retrieval.budget import BudgetedItem, apply_budget, count_tokens, truncate_snippet
from tessera.retrieval.pipeline import PipelineContext, RecallDegradedReason, RecallResult
from tessera.retrieval.pipeline import recall as _pipeline_recall
from tessera.vault import audit as vault_audit
from tessera.vault import capture as vault_capture
from tessera.vault import facets as vault_facets
from tessera.vault import people as vault_people
from tessera.vault import skills as vault_skills

# Input validation limits. These are hard caps; any payload exceeding
# them is rejected at the boundary with :class:`ValidationError` rather
# than being silently truncated — truncation would hide an adversarial
# or buggy caller from the operator.
_MAX_CONTENT_CHARS: Final[int] = 65_536
_MAX_QUERY_CHARS: Final[int] = 4_096
_MAX_REASON_CHARS: Final[int] = 1_024
_MIN_K: Final[int] = 1
_MAX_K: Final[int] = 100
_MIN_LIMIT: Final[int] = 1
_MAX_LIMIT: Final[int] = 100
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
# budgets but never larger — ``recall`` clamps its requested_budget to
# the declared ceiling.
CAPTURE_RESPONSE_BUDGET: Final[int] = 512
RECALL_RESPONSE_BUDGET: Final[int] = 6_000
SHOW_RESPONSE_BUDGET: Final[int] = 2_048
LIST_FACETS_RESPONSE_BUDGET: Final[int] = 2_048
STATS_RESPONSE_BUDGET: Final[int] = 1_024
FORGET_RESPONSE_BUDGET: Final[int] = 256
LEARN_SKILL_RESPONSE_BUDGET: Final[int] = 512
GET_SKILL_RESPONSE_BUDGET: Final[int] = 4_096
LIST_SKILLS_RESPONSE_BUDGET: Final[int] = 2_048
RESOLVE_PERSON_RESPONSE_BUDGET: Final[int] = 1_024
LIST_PEOPLE_RESPONSE_BUDGET: Final[int] = 2_048

# v0.3 tool input bounds. Skill names are user-visible identifiers, so
# we cap them well below content; descriptions sit between names and
# free-form content; mentions are conversational fragments and live at
# the same ceiling as names.
_MAX_SKILL_NAME_CHARS: Final[int] = 256
_MAX_SKILL_DESCRIPTION_CHARS: Final[int] = 1_024
_MAX_MENTION_CHARS: Final[int] = 256

# ULID shape: 26 chars Crockford base32. We accept the canonical upper
# alphabet only; the facets module mints via python-ulid which emits
# uppercase, and allowing lowercase would double the enumeration
# surface a stolen vault's ``show`` calls have to defend against.
_ULID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SOURCE_TOOL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ToolError(Exception):
    """Base class for MCP-boundary errors.

    Every subclass carries a stable ``code`` attribute so the
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
    (capture, show, list_facets, stats, forget) can be called without
    wiring an embedder or reranker. Tools that do require retrieval
    raise :class:`ValidationError` when ``pipeline`` is missing.
    """

    conn: sqlcipher3.Connection
    verified: VerifiedCapability
    vault_path: Path
    clock: Callable[[], int] = field(default_factory=lambda: _now_epoch)
    pipeline: PipelineContext | None = None
    event_log: EventLog | None = None


@dataclass(frozen=True, slots=True)
class ToolContract:
    """Executable contract for one public MCP tool.

    The stdio bridge and contract tests consume this directly so the
    tool catalogue, defaults, and JSON schemas cannot drift from the
    implementation without a test failure.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    response_budget_tokens: int


MCP_TOOL_CONTRACTS: Final[tuple[ToolContract, ...]] = (
    ToolContract(
        name="capture",
        description=(
            "Capture a new facet into the vault. Required args: content, facet_type. "
            "Optional args: source_tool, metadata, volatility, ttl_seconds."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "facet_type": {
                    "type": "string",
                    "enum": sorted(vault_facets.WRITABLE_FACET_TYPES),
                },
                "source_tool": {
                    "type": "string",
                    "pattern": _SOURCE_TOOL_PATTERN.pattern,
                },
                "metadata": {
                    "type": "object",
                    "maxProperties": _MAX_METADATA_KEYS,
                },
                "volatility": {
                    "type": "string",
                    "enum": sorted(vault_facets.WRITABLE_VOLATILITIES),
                    "default": "persistent",
                },
                "ttl_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": vault_facets.MAX_TTL_SECONDS["session"],
                },
            },
            "required": ["content", "facet_type"],
            "additionalProperties": False,
        },
        response_budget_tokens=CAPTURE_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="recall",
        description=(
            "Hybrid recall over every facet type the token can read unless facet_types "
            "is supplied. Optional k defaults to 10."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query_text": {"type": "string", "maxLength": _MAX_QUERY_CHARS},
                "k": {
                    "type": "integer",
                    "minimum": _MIN_K,
                    "maximum": _MAX_K,
                    "default": 10,
                },
                "facet_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": sorted(vault_facets.WRITABLE_FACET_TYPES),
                    },
                },
                "requested_budget_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": RECALL_RESPONSE_BUDGET,
                },
            },
            "required": ["query_text"],
            "additionalProperties": False,
        },
        response_budget_tokens=RECALL_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="show",
        description="Return one facet by external_id.",
        input_schema={
            "type": "object",
            "properties": {"external_id": {"type": "string", "pattern": _ULID_PATTERN.pattern}},
            "required": ["external_id"],
            "additionalProperties": False,
        },
        response_budget_tokens=SHOW_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_facets",
        description="List facets for one facet type. Optional limit defaults to 20.",
        input_schema={
            "type": "object",
            "properties": {
                "facet_type": {
                    "type": "string",
                    "enum": sorted(vault_facets.WRITABLE_FACET_TYPES),
                },
                "limit": {
                    "type": "integer",
                    "minimum": _MIN_LIMIT,
                    "maximum": _MAX_LIMIT,
                    "default": 20,
                },
                "since": {
                    "type": "integer",
                    "minimum": _MIN_SINCE_EPOCH,
                    "maximum": _MAX_SINCE_EPOCH,
                },
            },
            "required": ["facet_type"],
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_FACETS_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="stats",
        description="Return vault statistics, embedding health, and active model metadata.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        response_budget_tokens=STATS_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="forget",
        description="Soft-delete one facet by external_id. Optional reason records audit context.",
        input_schema={
            "type": "object",
            "properties": {
                "external_id": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                "reason": {"type": "string", "maxLength": _MAX_REASON_CHARS},
            },
            "required": ["external_id"],
            "additionalProperties": False,
        },
        response_budget_tokens=FORGET_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="learn_skill",
        description=(
            "Create a skill (named procedure markdown) the agent can recall later. "
            "Required args: name, description, procedure_md."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": _MAX_SKILL_NAME_CHARS},
                "description": {"type": "string", "maxLength": _MAX_SKILL_DESCRIPTION_CHARS},
                "procedure_md": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
            },
            "required": ["name", "description", "procedure_md"],
            "additionalProperties": False,
        },
        response_budget_tokens=LEARN_SKILL_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="get_skill",
        description="Fetch one skill by exact name. Returns null when no live skill matches.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": _MAX_SKILL_NAME_CHARS},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_budget_tokens=GET_SKILL_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_skills",
        description=(
            "List the agent's skills, ordered by name. Optional active_only=true filters "
            "out retired skills (default true). Optional limit defaults to 50."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "default": True},
                "limit": {
                    "type": "integer",
                    "minimum": _MIN_LIMIT,
                    "maximum": _MAX_LIMIT,
                    "default": 50,
                },
            },
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_SKILLS_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="resolve_person",
        description=(
            "Turn a free-form mention string into candidate person rows. Returns "
            "is_exact=true only when canonical-name or alias matches exactly single."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mention": {"type": "string", "maxLength": _MAX_MENTION_CHARS},
            },
            "required": ["mention"],
            "additionalProperties": False,
        },
        response_budget_tokens=RESOLVE_PERSON_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_people",
        description=(
            "List the agent's people roster, ordered by canonical name. Optional limit "
            "defaults to 50, optional since filters by created_at epoch."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": _MIN_LIMIT,
                    "maximum": _MAX_LIMIT,
                    "default": 50,
                },
                "since": {
                    "type": "integer",
                    "minimum": _MIN_SINCE_EPOCH,
                    "maximum": _MAX_SINCE_EPOCH,
                },
            },
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_PEOPLE_RESPONSE_BUDGET,
    ),
)


# ---- Response dataclasses -----------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    external_id: str
    is_duplicate: bool
    facet_type: str
    volatility: str
    ttl_seconds: int | None


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
    degraded_reason: RecallDegradedReason | None
    seed: int
    truncated: bool
    rerank_degraded: bool
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ShowResponse:
    external_id: str
    facet_type: str
    snippet: str
    captured_at: int
    source_tool: str
    embed_status: str
    token_count: int


@dataclass(frozen=True, slots=True)
class FacetSummary:
    external_id: str
    facet_type: str
    snippet: str
    captured_at: int
    source_tool: str
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


@dataclass(frozen=True, slots=True)
class ForgetResponse:
    external_id: str
    facet_type: str
    deleted_at: int


@dataclass(frozen=True, slots=True)
class LearnSkillResponse:
    external_id: str
    name: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class SkillView:
    """Full skill payload returned by ``get_skill``.

    Carries the procedure markdown verbatim — callers requesting the
    skill body get the canonical text. Token-budget enforcement
    truncates the procedure tail (with ``truncated=True``) when the
    budget cannot fit the full body.
    """

    external_id: str
    name: str
    description: str
    procedure_md: str
    active: bool
    disk_path: str | None
    captured_at: int
    embed_status: str
    truncated: bool
    token_count: int


@dataclass(frozen=True, slots=True)
class SkillSummary:
    external_id: str
    name: str
    description: str
    active: bool
    captured_at: int


@dataclass(frozen=True, slots=True)
class ListSkillsResponse:
    items: tuple[SkillSummary, ...]
    truncated: bool
    total_tokens: int


@dataclass(frozen=True, slots=True)
class PersonMatch:
    external_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    created_at: int


@dataclass(frozen=True, slots=True)
class ResolvePersonResponse:
    matches: tuple[PersonMatch, ...]
    is_exact: bool


@dataclass(frozen=True, slots=True)
class ListPeopleResponse:
    items: tuple[PersonMatch, ...]
    truncated: bool
    total_tokens: int


# ---- Tools ---------------------------------------------------------------


async def capture(
    tctx: ToolContext,
    *,
    content: str,
    facet_type: str,
    source_tool: str | None = None,
    metadata: dict[str, Any] | None = None,
    volatility: str = "persistent",
    ttl_seconds: int | None = None,
) -> CaptureResponse:
    """MCP ``capture`` — insert a facet.

    Delegates to :func:`tessera.vault.capture.capture` after write-scope
    enforcement. ``source_tool`` defaults to the capability's
    ``client_name`` when omitted; callers may override to attribute a
    capture to a specific sub-agent, but the capability's client_name
    is what lands in the audit row regardless. ``volatility`` per ADR
    0016 defaults to ``persistent``; callers writing working memory
    pass ``session`` or ``ephemeral`` and may override the default TTL
    inside the per-volatility ceiling.
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    _validate_facet_type(facet_type)
    _validate_metadata(metadata)
    _validate_volatility(volatility)
    _validate_ttl_seconds(ttl_seconds, volatility=volatility)
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type=facet_type)
    try:
        result = vault_capture.capture(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            facet_type=facet_type,
            content=content,
            source_tool=resolved_source,
            metadata=metadata,
            volatility=volatility,
            ttl_seconds=ttl_seconds,
        )
    except vault_facets.UnknownAgentError as exc:
        # Agent rows are the vault's stable root; a capability pointing
        # at a vanished agent is a data-integrity break that the MCP
        # boundary surfaces as a storage error with a stable code.
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    except (vault_facets.UnsupportedVolatilityError, vault_facets.InvalidTTLError) as exc:
        raise ValidationError(str(exc)) from exc
    return CaptureResponse(
        external_id=result.external_id,
        is_duplicate=result.is_duplicate,
        facet_type=facet_type,
        volatility=result.volatility,
        ttl_seconds=result.ttl_seconds,
    )


async def recall(
    tctx: ToolContext,
    *,
    query_text: str,
    k: int,
    facet_types: Sequence[str] | None = None,
    requested_budget_tokens: int | None = None,
) -> RecallResponse:
    """MCP ``recall`` — hybrid retrieval + rerank + SWCR + budget.

    ``facet_types`` defaults to the pipeline's configured set. Under
    the post-reframe surface the dispatcher populates that set with
    every v0.1 facet type the caller is scoped to read, so a bare
    ``recall`` produces a cross-facet bundle. Read-scope is checked
    per requested facet type — a partial scope denial raises rather
    than returning a filtered subset, so the caller cannot
    accidentally observe a narrower result than they asked for.
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
        degraded_reason=result.degraded_reason,
        seed=result.seed,
        truncated=truncated or result.truncated,
        rerank_degraded=result.rerank_degraded,
        total_tokens=sum(m.token_count for m in trimmed),
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
        source_tool=facet.source_tool,
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
            source_tool=r.source_tool,
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


async def forget(
    tctx: ToolContext,
    *,
    external_id: str,
    reason: str | None = None,
) -> ForgetResponse:
    """MCP ``forget`` — soft-delete one facet, emit an audit entry.

    The capability must carry ``write`` scope for the target facet's
    type. The facet row keeps its audit trail; re-capturing the same
    content resurrects the row via the dedup path in
    :func:`tessera.vault.facets.insert`. Hard delete is explicit and
    only reachable via the ``tessera vault vacuum`` CLI path.
    """

    _validate_ulid(external_id)
    if reason is not None:
        _validate_length("reason", reason, _MAX_REASON_CHARS, allow_empty=False)
    facet = vault_facets.get(tctx.conn, external_id)
    if facet is None:
        raise ValidationError(f"facet {external_id!r} does not exist")
    if facet.is_deleted:
        raise ValidationError(f"facet {external_id!r} is already forgotten")
    _require_scope(tctx, op="write", facet_type=facet.facet_type)
    deleted = vault_facets.soft_delete(tctx.conn, external_id)
    if not deleted:
        # A concurrent forget raced us between the get() above and the
        # soft_delete() — surface as validation so the caller sees the
        # same error shape as the "already forgotten" path above.
        raise ValidationError(f"facet {external_id!r} is already forgotten")
    now = tctx.clock()
    vault_audit.write(
        tctx.conn,
        op="forget",
        actor=tctx.verified.client_name,
        agent_id=tctx.verified.agent_id,
        target_external_id=external_id,
        payload={"facet_type": facet.facet_type, "reason": reason},
        at=now,
    )
    return ForgetResponse(
        external_id=external_id,
        facet_type=facet.facet_type,
        deleted_at=now,
    )


async def learn_skill(
    tctx: ToolContext,
    *,
    name: str,
    description: str,
    procedure_md: str,
    source_tool: str | None = None,
) -> LearnSkillResponse:
    """MCP ``learn_skill`` — create a named skill.

    Write scope on ``skill`` is required. The underlying
    ``vault.skills.create_skill`` enforces per-agent name uniqueness
    and rides on the facets ``UNIQUE(agent_id, content_hash)`` so two
    skills cannot share a procedure body. ``source_tool`` defaults to
    the capability's ``client_name`` when omitted, mirroring capture.
    """

    _validate_length("name", name, _MAX_SKILL_NAME_CHARS, allow_empty=False)
    _validate_length("description", description, _MAX_SKILL_DESCRIPTION_CHARS, allow_empty=True)
    _validate_length("procedure_md", procedure_md, _MAX_CONTENT_CHARS, allow_empty=False)
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="skill")
    try:
        external_id, is_new = vault_skills.create_skill(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            name=name,
            description=description,
            procedure_md=procedure_md,
            source_tool=resolved_source,
        )
    except vault_skills.DuplicateSkillNameError as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return LearnSkillResponse(external_id=external_id, name=name.strip(), is_new=is_new)


async def get_skill(tctx: ToolContext, *, name: str) -> SkillView | None:
    """MCP ``get_skill`` — fetch a skill by exact name.

    Returns ``None`` when the agent has no live skill with that name.
    Long procedure markdown is truncated to fit the per-tool budget;
    the response carries ``truncated=True`` when truncation fired so
    the caller can request the full body via ``show`` if needed.
    """

    _validate_length("name", name, _MAX_SKILL_NAME_CHARS, allow_empty=False)
    _require_scope(tctx, op="read", facet_type="skill")
    skill = vault_skills.get_by_name(tctx.conn, agent_id=tctx.verified.agent_id, name=name)
    if skill is None:
        return None
    body = skill.procedure_md
    body_tokens = count_tokens(body)
    truncated = False
    if body_tokens > GET_SKILL_RESPONSE_BUDGET - 64:
        body = truncate_snippet(body, max_tokens=GET_SKILL_RESPONSE_BUDGET - 64)
        truncated = True
        body_tokens = count_tokens(body)
    return SkillView(
        external_id=skill.external_id,
        name=skill.name,
        description=skill.description,
        procedure_md=body,
        active=skill.active,
        disk_path=skill.disk_path,
        captured_at=skill.captured_at,
        embed_status=skill.embed_status,
        truncated=truncated,
        token_count=body_tokens,
    )


async def list_skills(
    tctx: ToolContext,
    *,
    active_only: bool = True,
    limit: int = 50,
) -> ListSkillsResponse:
    """MCP ``list_skills`` — paginated metadata for skill rows."""

    _validate_limit(limit)
    _require_scope(tctx, op="read", facet_type="skill")
    rows = vault_skills.list_skills(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        active_only=active_only,
        limit=limit,
    )
    summaries = [
        SkillSummary(
            external_id=s.external_id,
            name=s.name,
            description=s.description,
            active=s.active,
            captured_at=s.captured_at,
        )
        for s in rows
    ]
    items, truncated, total_tokens = _budget_skill_summaries(summaries)
    return ListSkillsResponse(items=items, truncated=truncated, total_tokens=total_tokens)


async def resolve_person(
    tctx: ToolContext,
    *,
    mention: str,
) -> ResolvePersonResponse:
    """MCP ``resolve_person`` — map a mention string to candidate rows.

    Read scope on ``person`` required. The result mirrors
    :class:`vault.people.ResolveResult`: ``is_exact=True`` only when a
    single canonical-name or alias hit lands; otherwise the caller
    receives a list to disambiguate.
    """

    _validate_length("mention", mention, _MAX_MENTION_CHARS, allow_empty=False)
    _require_scope(tctx, op="read", facet_type="person")
    result = vault_people.resolve(tctx.conn, agent_id=tctx.verified.agent_id, mention=mention)
    matches = tuple(_person_match_view(p) for p in result.matches)
    return ResolvePersonResponse(matches=matches, is_exact=result.is_exact)


async def list_people(
    tctx: ToolContext,
    *,
    limit: int = 50,
    since: int | None = None,
) -> ListPeopleResponse:
    """MCP ``list_people`` — paginated people roster."""

    _validate_limit(limit)
    if since is not None:
        _validate_since(since)
    _require_scope(tctx, op="read", facet_type="person")
    rows = vault_people.list_by_agent(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        limit=limit,
        since=since,
    )
    matches = [_person_match_view(p) for p in rows]
    items, truncated, total_tokens = _budget_person_matches(matches)
    return ListPeopleResponse(items=items, truncated=truncated, total_tokens=total_tokens)


# ---- Helpers -------------------------------------------------------------


def _require_scope(tctx: ToolContext, *, op: ScopeOp, facet_type: str) -> None:
    scope: Scope = tctx.verified.scope
    if scope.allows(op=op, facet_type=facet_type):
        return
    now = tctx.clock()
    record_scope_denial(
        tctx.conn,
        token_id=tctx.verified.token_id,
        client_name=tctx.verified.client_name,
        required_op=op,
        required_facet_type=facet_type,
        now_epoch=now,
    )
    if tctx.event_log is not None:
        # Events.db mirror of the audit row. Audit is the forensic
        # record of record; events.db is the operational surface the
        # diagnostic bundle scrapes. Both fire on every denial so the
        # two records stay aligned under `tessera doctor --collect`.
        tctx.event_log.emit(
            level="warn",
            category="auth",
            event="scope_denied",
            attrs={
                "token_id": tctx.verified.token_id,
                "client_name": tctx.verified.client_name,
                "required_op": op,
                "required_facet_type": facet_type,
            },
            at=now,
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
    if facet_type not in vault_facets.WRITABLE_FACET_TYPES:
        raise ValidationError(
            f"facet_type {facet_type!r} not in {sorted(vault_facets.WRITABLE_FACET_TYPES)}"
        )


def _validate_volatility(volatility: str) -> None:
    if not isinstance(volatility, str):
        raise ValidationError("volatility must be a string")
    if volatility not in vault_facets.WRITABLE_VOLATILITIES:
        raise ValidationError(
            f"volatility {volatility!r} not in {sorted(vault_facets.WRITABLE_VOLATILITIES)}"
        )


def _validate_ttl_seconds(ttl_seconds: int | None, *, volatility: str) -> None:
    if ttl_seconds is None:
        return
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValidationError("ttl_seconds must be an integer")
    if ttl_seconds <= 0:
        raise ValidationError(f"ttl_seconds={ttl_seconds} must be positive")
    if volatility == "persistent":
        raise ValidationError("ttl_seconds is not allowed when volatility='persistent'")
    ceiling = vault_facets.MAX_TTL_SECONDS.get(volatility)
    if ceiling is not None and ttl_seconds > ceiling:
        raise ValidationError(
            f"ttl_seconds={ttl_seconds} exceeds {volatility} ceiling of {ceiling}s"
        )


def _validate_source_tool(source_tool: str) -> None:
    if not _SOURCE_TOOL_PATTERN.match(source_tool):
        raise ValidationError(
            f"source_tool {source_tool!r} must match {_SOURCE_TOOL_PATTERN.pattern}"
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


def _person_match_view(person: vault_people.Person) -> PersonMatch:
    return PersonMatch(
        external_id=person.external_id,
        canonical_name=person.canonical_name,
        aliases=person.aliases,
        created_at=person.created_at,
    )


def _budget_skill_summaries(
    summaries: list[SkillSummary],
) -> tuple[tuple[SkillSummary, ...], bool, int]:
    """Apply the list_skills budget to skill summaries.

    Each summary's token cost is the description plus a per-row
    overhead for the name + flags. The trim falls back to the trailing
    items so the caller sees the alphabetically-earliest skills first.
    """

    items: list[BudgetedItem] = []
    for s in summaries:
        per_row_overhead = 32
        items.append(
            BudgetedItem(
                key=s.external_id,
                snippet=s.description,
                token_count=(count_tokens(s.description) + count_tokens(s.name) + per_row_overhead),
            )
        )
    trimmed = apply_budget(items, total_budget=LIST_SKILLS_RESPONSE_BUDGET)
    kept_keys = {item.key for item in trimmed.items}
    kept = tuple(s for s in summaries if s.external_id in kept_keys)
    total = sum(item.token_count for item in trimmed.items)
    return kept, trimmed.truncated, total


def _budget_person_matches(
    matches: list[PersonMatch],
) -> tuple[tuple[PersonMatch, ...], bool, int]:
    """Apply the list_people budget to person rows.

    Token cost is the canonical name + every alias + a per-row
    overhead. People rows are small, so truncation only fires on
    pathologically large rosters.
    """

    items: list[BudgetedItem] = []
    for p in matches:
        per_row_overhead = 24
        alias_tokens = sum(count_tokens(a) for a in p.aliases)
        snippet = p.canonical_name + (" " + " ".join(p.aliases) if p.aliases else "")
        items.append(
            BudgetedItem(
                key=p.external_id,
                snippet=snippet,
                token_count=count_tokens(p.canonical_name) + alias_tokens + per_row_overhead,
            )
        )
    trimmed = apply_budget(items, total_budget=LIST_PEOPLE_RESPONSE_BUDGET)
    kept_keys = {item.key for item in trimmed.items}
    kept = tuple(p for p in matches if p.external_id in kept_keys)
    total = sum(item.token_count for item in trimmed.items)
    return kept, trimmed.truncated, total


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
        SELECT source_tool, COUNT(*)
          FROM facets
         WHERE agent_id = ? AND is_deleted = 0
      GROUP BY source_tool
      ORDER BY source_tool
        """,
        (agent_id,),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


__all__ = [
    "CAPTURE_RESPONSE_BUDGET",
    "FORGET_RESPONSE_BUDGET",
    "GET_SKILL_RESPONSE_BUDGET",
    "LEARN_SKILL_RESPONSE_BUDGET",
    "LIST_FACETS_RESPONSE_BUDGET",
    "LIST_PEOPLE_RESPONSE_BUDGET",
    "LIST_SKILLS_RESPONSE_BUDGET",
    "MCP_TOOL_CONTRACTS",
    "RECALL_RESPONSE_BUDGET",
    "RESOLVE_PERSON_RESPONSE_BUDGET",
    "SHOW_RESPONSE_BUDGET",
    "STATS_RESPONSE_BUDGET",
    "ActiveModel",
    "BudgetExceeded",
    "CaptureResponse",
    "EmbedHealth",
    "FacetSummary",
    "ForgetResponse",
    "LearnSkillResponse",
    "ListFacetsResponse",
    "ListPeopleResponse",
    "ListSkillsResponse",
    "PersonMatch",
    "RecallMatchView",
    "RecallResponse",
    "ResolvePersonResponse",
    "ScopeDenied",
    "ShowResponse",
    "SkillSummary",
    "SkillView",
    "StatsResponse",
    "StorageError",
    "ToolContext",
    "ToolContract",
    "ToolError",
    "ValidationError",
    "capture",
    "forget",
    "get_skill",
    "learn_skill",
    "list_facets",
    "list_people",
    "list_skills",
    "recall",
    "resolve_person",
    "show",
    "stats",
]
