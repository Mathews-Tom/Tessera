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
from tessera.vault import agent_profiles as vault_agent_profiles
from tessera.vault import audit as vault_audit
from tessera.vault import automations as vault_automations
from tessera.vault import capture as vault_capture
from tessera.vault import compiled as vault_compiled
from tessera.vault import facets as vault_facets
from tessera.vault import people as vault_people
from tessera.vault import retrospectives as vault_retrospectives
from tessera.vault import skills as vault_skills
from tessera.vault import verification as vault_verification

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
REGISTER_AGENT_PROFILE_RESPONSE_BUDGET: Final[int] = 512
GET_AGENT_PROFILE_RESPONSE_BUDGET: Final[int] = 4_096
LIST_AGENT_PROFILES_RESPONSE_BUDGET: Final[int] = 4_096
REGISTER_CHECKLIST_RESPONSE_BUDGET: Final[int] = 512
RECORD_RETROSPECTIVE_RESPONSE_BUDGET: Final[int] = 512
LIST_CHECKS_FOR_AGENT_RESPONSE_BUDGET: Final[int] = 4_096
REGISTER_COMPILED_ARTIFACT_RESPONSE_BUDGET: Final[int] = 512
GET_COMPILED_ARTIFACT_RESPONSE_BUDGET: Final[int] = 6_000
LIST_COMPILE_SOURCES_RESPONSE_BUDGET: Final[int] = 6_000
REGISTER_AUTOMATION_RESPONSE_BUDGET: Final[int] = 512
RECORD_AUTOMATION_RUN_RESPONSE_BUDGET: Final[int] = 256

# Compile target string bound. Targets are short slugs (e.g.,
# ``playbook_main``); 128 chars is a generous ceiling that still
# rejects pathological inputs at the MCP boundary.
_MAX_COMPILE_TARGET_CHARS: Final[int] = 128

# v0.3 tool input bounds. Skill names are user-visible identifiers, so
# we cap them well below content; descriptions sit between names and
# free-form content; mentions are conversational fragments and live at
# the same ceiling as names.
_MAX_SKILL_NAME_CHARS: Final[int] = 256
_MAX_SKILL_DESCRIPTION_CHARS: Final[int] = 1_024
_MAX_MENTION_CHARS: Final[int] = 256

# Agent profile bounds. Profile content is the human-readable narrative;
# the structured metadata limits live in :mod:`tessera.vault.agent_profiles`
# so this boundary only enforces an outer envelope before delegation.
_MAX_AGENT_PROFILE_CONTENT_CHARS: Final[int] = _MAX_CONTENT_CHARS

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
    ToolContract(
        name="register_agent_profile",
        description=(
            "Register an agent_profile facet describing what an autonomous worker does. "
            "Required args: content, metadata (purpose, inputs, outputs, cadence, skill_refs). "
            "Optional metadata.verification_ref links a verification_checklist facet. "
            "Updates agents.profile_facet_external_id to the new profile's id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_AGENT_PROFILE_CONTENT_CHARS},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "purpose": {"type": "string"},
                        "inputs": {"type": "array", "items": {"type": "string"}},
                        "outputs": {"type": "array", "items": {"type": "string"}},
                        "cadence": {"type": "string"},
                        "skill_refs": {"type": "array", "items": {"type": "string"}},
                        "verification_ref": {"type": ["string", "null"]},
                    },
                    "required": ["purpose", "inputs", "outputs", "cadence", "skill_refs"],
                    "additionalProperties": False,
                },
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
                "set_active_link": {"type": "boolean", "default": True},
            },
            "required": ["content", "metadata"],
            "additionalProperties": False,
        },
        response_budget_tokens=REGISTER_AGENT_PROFILE_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="get_agent_profile",
        description=(
            "Fetch one agent_profile facet by external_id. Returns null when no live "
            "profile matches. The active-link flag indicates whether the profile is the "
            "one currently linked from agents.profile_facet_external_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "external_id": {"type": "string", "pattern": _ULID_PATTERN.pattern},
            },
            "required": ["external_id"],
            "additionalProperties": False,
        },
        response_budget_tokens=GET_AGENT_PROFILE_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_agent_profiles",
        description=(
            "List the calling agent's agent_profile facets, ordered by capture time "
            "descending. Optional limit defaults to 20."
        ),
        input_schema={
            "type": "object",
            "properties": {
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
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_AGENT_PROFILES_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="register_checklist",
        description=(
            "Register a verification_checklist facet — the pre-delivery gate an "
            "agent runs before declaring a task done. Required args: content, "
            "metadata (agent_ref, trigger, checks[{id, statement, severity}], "
            "pass_criteria). Tessera stores the checklist; the agent or its "
            "caller-side runner executes it (ADR 0018 boundary)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "agent_ref": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                        "trigger": {"type": "string"},
                        "checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "statement": {"type": "string"},
                                    "severity": {
                                        "type": "string",
                                        "enum": ["blocker", "warning", "informational"],
                                    },
                                },
                                "required": ["id", "statement", "severity"],
                                "additionalProperties": False,
                            },
                        },
                        "pass_criteria": {"type": "string"},
                    },
                    "required": ["agent_ref", "trigger", "checks", "pass_criteria"],
                    "additionalProperties": False,
                },
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
            },
            "required": ["content", "metadata"],
            "additionalProperties": False,
        },
        response_budget_tokens=REGISTER_CHECKLIST_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="record_retrospective",
        description=(
            "Record a retrospective facet — the post-run reflection on what worked, "
            "what gapped, and what the agent or user wants to change next time. "
            "Required args: content, metadata (agent_ref, task_id, went_well[], "
            "gaps[], changes[{target, change}], outcome)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "agent_ref": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                        "task_id": {"type": "string"},
                        "went_well": {"type": "array", "items": {"type": "string"}},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                        "changes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "target": {"type": "string"},
                                    "change": {"type": "string"},
                                },
                                "required": ["target", "change"],
                                "additionalProperties": False,
                            },
                        },
                        "outcome": {
                            "type": "string",
                            "enum": ["success", "partial", "failure"],
                        },
                    },
                    "required": [
                        "agent_ref",
                        "task_id",
                        "went_well",
                        "gaps",
                        "changes",
                        "outcome",
                    ],
                    "additionalProperties": False,
                },
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
            },
            "required": ["content", "metadata"],
            "additionalProperties": False,
        },
        response_budget_tokens=RECORD_RETROSPECTIVE_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_checks_for_agent",
        description=(
            "Resolve an agent_profile's verification_ref to the live checklist row. "
            "Returns null when the profile has no verification_ref or the linked "
            "checklist is missing / soft-deleted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "profile_external_id": {
                    "type": "string",
                    "pattern": _ULID_PATTERN.pattern,
                },
            },
            "required": ["profile_external_id"],
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_CHECKS_FOR_AGENT_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="register_compiled_artifact",
        description=(
            "Register a compiled artifact (AgenticOS Playbook). The caller-side "
            "compiler pre-reads sources via list_compile_sources or recall, "
            "synthesises the narrative, and posts the rendered content here. "
            "Tessera stores; the caller compiles. Required args: content, "
            "source_facets (list of source ULIDs), compiler_version. Optional: "
            "artifact_type (defaults to 'playbook'), metadata, source_tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "source_facets": {
                    "type": "array",
                    "items": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                },
                "compiler_version": {"type": "string"},
                "artifact_type": {"type": "string"},
                "metadata": {
                    "type": "object",
                    "maxProperties": _MAX_METADATA_KEYS,
                },
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
            },
            "required": ["content", "source_facets", "compiler_version"],
            "additionalProperties": False,
        },
        response_budget_tokens=REGISTER_COMPILED_ARTIFACT_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="get_compiled_artifact",
        description=(
            "Fetch one compiled artifact by external_id. Returns the rendered "
            "content + provenance (source_facets, compiler_version, is_stale). "
            "Cross-agent reads return null even when the ULID is leaked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "external_id": {"type": "string", "pattern": _ULID_PATTERN.pattern},
            },
            "required": ["external_id"],
            "additionalProperties": False,
        },
        response_budget_tokens=GET_COMPILED_ARTIFACT_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="list_compile_sources",
        description=(
            "Enumerate source facets tagged for a compile target. The caller "
            "tags eligible sources by setting metadata.compile_into = [target] "
            "on the source facet; this tool returns those tagged rows so the "
            "caller-side compiler can read the inputs it needs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "maxLength": _MAX_COMPILE_TARGET_CHARS},
                "limit": {
                    "type": "integer",
                    "minimum": _MIN_LIMIT,
                    "maximum": _MAX_LIMIT,
                    "default": 50,
                },
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        response_budget_tokens=LIST_COMPILE_SOURCES_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="register_automation",
        description=(
            "Register an automation facet — the storage-only record of a "
            "scheduled-or-triggered task that a caller-side runner (Claude "
            "Code /schedule, OpenClaw HEARTBEAT, cron, systemd, GitHub "
            "Actions, custom shell loop) executes. Tessera registers; "
            "runners run (ADR 0020 boundary). Required args: content, "
            "metadata (agent_ref, trigger_spec, cadence, runner). Optional "
            "metadata: last_run (ISO-8601), last_result. There is no "
            "scheduler runtime, no outbound trigger, no in-process timer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": _MAX_CONTENT_CHARS},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "agent_ref": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                        "trigger_spec": {"type": "string"},
                        "cadence": {"type": "string"},
                        "runner": {"type": "string"},
                        "last_run": {"type": "string"},
                        "last_result": {"type": "string"},
                    },
                    "required": ["agent_ref", "trigger_spec", "cadence", "runner"],
                    "additionalProperties": False,
                },
                "source_tool": {"type": "string", "pattern": _SOURCE_TOOL_PATTERN.pattern},
            },
            "required": ["content", "metadata"],
            "additionalProperties": False,
        },
        response_budget_tokens=REGISTER_AUTOMATION_RESPONSE_BUDGET,
    ),
    ToolContract(
        name="record_automation_run",
        description=(
            "Update last_run + last_result on an existing automation after the "
            "runner fires. The runner is the source of truth for run history; "
            "Tessera stores the receipt. Required args: external_id (ULID of the "
            "automation), last_run (ISO-8601 timestamp), last_result (free-form, "
            "or one of 'success' | 'partial' | 'failure'). Cross-agent updates "
            "are blocked at the storage layer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "external_id": {"type": "string", "pattern": _ULID_PATTERN.pattern},
                "last_run": {"type": "string"},
                "last_result": {"type": "string"},
            },
            "required": ["external_id", "last_run", "last_result"],
            "additionalProperties": False,
        },
        response_budget_tokens=RECORD_AUTOMATION_RUN_RESPONSE_BUDGET,
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


@dataclass(frozen=True, slots=True)
class RegisterAgentProfileResponse:
    external_id: str
    is_new: bool
    is_active_link: bool


@dataclass(frozen=True, slots=True)
class AgentProfileView:
    """Full agent_profile payload returned by ``get_agent_profile``."""

    external_id: str
    content: str
    purpose: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    cadence: str
    skill_refs: tuple[str, ...]
    verification_ref: str | None
    captured_at: int
    embed_status: str
    is_active_link: bool
    truncated: bool
    token_count: int


@dataclass(frozen=True, slots=True)
class AgentProfileSummary:
    external_id: str
    purpose: str
    cadence: str
    skill_refs: tuple[str, ...]
    captured_at: int
    is_active_link: bool


@dataclass(frozen=True, slots=True)
class ListAgentProfilesResponse:
    items: tuple[AgentProfileSummary, ...]
    truncated: bool
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RegisterChecklistResponse:
    external_id: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class ChecklistCheckView:
    id: str
    statement: str
    severity: str


@dataclass(frozen=True, slots=True)
class ChecklistView:
    """Full verification_checklist payload returned by ``list_checks_for_agent``."""

    external_id: str
    content: str
    agent_ref: str
    trigger: str
    checks: tuple[ChecklistCheckView, ...]
    pass_criteria: str
    captured_at: int
    embed_status: str
    truncated: bool
    token_count: int


@dataclass(frozen=True, slots=True)
class RecordRetrospectiveResponse:
    external_id: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class RegisterCompiledArtifactResponse:
    external_id: str
    artifact_type: str
    source_count: int


@dataclass(frozen=True, slots=True)
class RegisterAutomationResponse:
    external_id: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class RecordAutomationRunResponse:
    external_id: str
    last_run: str
    last_result: str


@dataclass(frozen=True, slots=True)
class CompiledArtifactView:
    """Full compiled-artifact payload returned by ``get_compiled_artifact``."""

    external_id: str
    content: str
    artifact_type: str
    source_facets: tuple[str, ...]
    compiler_version: str
    compiled_at: int
    is_stale: bool
    truncated: bool
    token_count: int


@dataclass(frozen=True, slots=True)
class CompileSourceView:
    external_id: str
    facet_type: str
    snippet: str
    captured_at: int
    token_count: int


@dataclass(frozen=True, slots=True)
class ListCompileSourcesResponse:
    items: tuple[CompileSourceView, ...]
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
    if facet_type == "agent_profile":
        # ADR 0017: agent_profile carries a structured metadata contract
        # that the generic capture path does not enforce. Routing writes
        # exclusively through ``register_agent_profile`` keeps every
        # stored profile parseable by the read tools and prevents a
        # write-scoped caller from poisoning subsequent reads with a
        # row whose metadata fails ``validate_metadata`` on retrieval.
        raise ValidationError(
            "agent_profile facets must be written via register_agent_profile, "
            "not the generic capture tool"
        )
    if facet_type == "automation":
        # ADR 0020: same structural concern as agent_profile —
        # automation carries a closed metadata contract (agent_ref,
        # trigger_spec, cadence, runner, optional last_run /
        # last_result) that ``vault.automations.validate_metadata``
        # enforces. The generic capture path skips that validation,
        # so a write-scoped caller could plant an automation with a
        # malformed shape that later breaks ``_row_to_automation``
        # for every read. The storage-only registry is only useful
        # if every stored row is parseable; routing writes
        # exclusively through ``register_automation`` upholds that.
        raise ValidationError(
            "automation facets must be written via register_automation, "
            "not the generic capture tool"
        )
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


async def register_agent_profile(
    tctx: ToolContext,
    *,
    content: str,
    metadata: dict[str, Any],
    source_tool: str | None = None,
    set_active_link: bool = True,
) -> RegisterAgentProfileResponse:
    """MCP ``register_agent_profile`` — create an agent_profile facet.

    Write scope on ``agent_profile`` is required. The structured
    metadata shape is enforced by
    :func:`tessera.vault.agent_profiles.validate_metadata` per ADR
    0017. The active link on ``agents.profile_facet_external_id``
    moves to the new profile unless the caller passes
    ``set_active_link=False`` (useful for staging a draft without
    swapping the canonical pointer in the same call).
    """

    _validate_length("content", content, _MAX_AGENT_PROFILE_CONTENT_CHARS, allow_empty=False)
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="agent_profile")
    try:
        external_id, is_new = vault_agent_profiles.register(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            content=content,
            metadata=metadata,
            source_tool=resolved_source,
            set_active_link=set_active_link,
        )
    except vault_agent_profiles.InvalidAgentProfileMetadataError as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    # Reflect ground truth: even when the caller staged a draft with
    # ``set_active_link=False``, the new facet may already be the
    # canonical row if it dedupes to the currently linked profile.
    # Read the link unconditionally so the response cannot mislabel.
    is_active = (
        vault_agent_profiles.read_active_link(tctx.conn, agent_id=tctx.verified.agent_id)
        == external_id
    )
    return RegisterAgentProfileResponse(
        external_id=external_id,
        is_new=is_new,
        is_active_link=is_active,
    )


async def get_agent_profile(
    tctx: ToolContext,
    *,
    external_id: str,
) -> AgentProfileView | None:
    """MCP ``get_agent_profile`` — fetch one profile by external_id.

    Returns ``None`` when no live profile matches. Read scope on
    ``agent_profile`` is required. Cross-agent reads are blocked here
    by an explicit agent-id check after the row lookup so a token
    scoped for one agent cannot peek at another agent's profile by
    guessing its ULID.
    """

    _validate_ulid(external_id)
    _require_scope(tctx, op="read", facet_type="agent_profile")
    profile = vault_agent_profiles.get(tctx.conn, external_id=external_id)
    if profile is None:
        return None
    if profile.agent_id != tctx.verified.agent_id:
        return None
    body = profile.content
    body_tokens = count_tokens(body)
    truncated = False
    overhead = 128
    if body_tokens > GET_AGENT_PROFILE_RESPONSE_BUDGET - overhead:
        body = truncate_snippet(body, max_tokens=GET_AGENT_PROFILE_RESPONSE_BUDGET - overhead)
        truncated = True
        body_tokens = count_tokens(body)
    meta = profile.metadata
    return AgentProfileView(
        external_id=profile.external_id,
        content=body,
        purpose=meta.purpose,
        inputs=meta.inputs,
        outputs=meta.outputs,
        cadence=meta.cadence,
        skill_refs=meta.skill_refs,
        verification_ref=meta.verification_ref,
        captured_at=profile.captured_at,
        embed_status=profile.embed_status,
        is_active_link=profile.is_active_link,
        truncated=truncated,
        token_count=body_tokens,
    )


async def list_agent_profiles(
    tctx: ToolContext,
    *,
    limit: int = 20,
    since: int | None = None,
) -> ListAgentProfilesResponse:
    """MCP ``list_agent_profiles`` — paginated metadata of profile rows."""

    _validate_limit(limit)
    if since is not None:
        _validate_since(since)
    _require_scope(tctx, op="read", facet_type="agent_profile")
    rows = vault_agent_profiles.list_for_agent(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        limit=limit,
        since=since,
    )
    summaries = [
        AgentProfileSummary(
            external_id=p.external_id,
            purpose=p.metadata.purpose,
            cadence=p.metadata.cadence,
            skill_refs=p.metadata.skill_refs,
            captured_at=p.captured_at,
            is_active_link=p.is_active_link,
        )
        for p in rows
    ]
    items, truncated, total_tokens = _budget_agent_profile_summaries(summaries)
    return ListAgentProfilesResponse(
        items=items,
        truncated=truncated,
        total_tokens=total_tokens,
    )


async def register_checklist(
    tctx: ToolContext,
    *,
    content: str,
    metadata: dict[str, Any],
    source_tool: str | None = None,
) -> RegisterChecklistResponse:
    """MCP ``register_checklist`` — create a verification_checklist facet.

    Write scope on ``verification_checklist`` is required. The
    ``agent_ref`` in metadata must be the ULID of an agent_profile
    facet owned by the same agent — the storage layer enforces the
    shape; the MCP boundary additionally guards cross-agent
    references so a write-scoped caller cannot plant a checklist
    that references another agent's profile.
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="verification_checklist")
    agent_ref = metadata.get("agent_ref")
    if isinstance(agent_ref, str):
        _enforce_same_agent_profile_ref(tctx, agent_ref)
    try:
        external_id, is_new = vault_verification.register(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            content=content,
            metadata=metadata,
            source_tool=resolved_source,
        )
    except vault_verification.InvalidChecklistMetadataError as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return RegisterChecklistResponse(external_id=external_id, is_new=is_new)


async def record_retrospective(
    tctx: ToolContext,
    *,
    content: str,
    metadata: dict[str, Any],
    source_tool: str | None = None,
) -> RecordRetrospectiveResponse:
    """MCP ``record_retrospective`` — create a retrospective facet.

    Write scope on ``retrospective`` is required. Retrospectives are
    immutable per task by design — re-recording with byte-identical
    content collapses through the dedup path and returns the
    existing facet with ``is_new=False``. The ``agent_ref`` is
    guarded against cross-agent leakage just like
    ``register_checklist``.
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="retrospective")
    agent_ref = metadata.get("agent_ref")
    if isinstance(agent_ref, str):
        _enforce_same_agent_profile_ref(tctx, agent_ref)
    try:
        external_id, is_new = vault_retrospectives.record(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            content=content,
            metadata=metadata,
            source_tool=resolved_source,
        )
    except vault_retrospectives.InvalidRetrospectiveMetadataError as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return RecordRetrospectiveResponse(external_id=external_id, is_new=is_new)


async def register_automation(
    tctx: ToolContext,
    *,
    content: str,
    metadata: dict[str, Any],
    source_tool: str | None = None,
) -> RegisterAutomationResponse:
    """MCP ``register_automation`` — create an ``automation`` facet.

    Write scope on ``automation`` is required. The ``agent_ref`` in
    metadata must be the ULID of an ``agent_profile`` facet owned by
    the same agent — the cross-agent guard runs at the MCP boundary
    so a write-scoped caller cannot register an automation that
    references another agent's profile.

    Tessera **stores** the automation; the runner identified by
    ``metadata.runner`` (cron, /schedule, HEARTBEAT, etc.) executes
    it. There is no scheduler runtime, no outbound trigger, no
    in-process timer (ADR 0020 §Boundary statement).
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="automation")
    agent_ref = metadata.get("agent_ref")
    if isinstance(agent_ref, str):
        _enforce_same_agent_profile_ref(tctx, agent_ref)
    try:
        external_id, is_new = vault_automations.register(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            content=content,
            metadata=metadata,
            source_tool=resolved_source,
        )
    except vault_automations.InvalidAutomationMetadataError as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return RegisterAutomationResponse(external_id=external_id, is_new=is_new)


async def record_automation_run(
    tctx: ToolContext,
    *,
    external_id: str,
    last_run: str,
    last_result: str,
) -> RecordAutomationRunResponse:
    """MCP ``record_automation_run`` — update last_run/last_result on an
    existing automation.

    Write scope on ``automation`` is required. Tessera does not parse
    ``last_run`` beyond the ISO-8601 shape check; the runner is the
    source of truth for run history. Cross-agent updates are blocked
    at the storage layer (the SQL filter on ``agent_id`` raises
    :class:`UnknownAutomationError` rather than returning a no-op).

    Per V0.5-P5 §S4 boundary, the audit row carries a bucketed
    ``result_bucket`` (``success`` / ``partial`` / ``failure`` /
    ``other``) and the ISO-8601 timestamp; free-form caller prose
    never enters the audit payload.
    """

    _validate_ulid(external_id)
    _require_scope(tctx, op="write", facet_type="automation")
    try:
        vault_automations.record_run(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            external_id=external_id,
            last_run=last_run,
            last_result=last_result,
        )
    except vault_automations.UnknownAutomationError as exc:
        # Cross-agent reads return null elsewhere on the surface; here
        # we surface as a validation error because the caller named a
        # specific external_id and we owe them an explicit denial
        # rather than a silent no-op.
        raise ValidationError(str(exc)) from exc
    except vault_automations.CorruptAutomationRowError as exc:
        # Distinct from caller-input validation: the stored row is
        # malformed (drift from the ADR-0020 contract or a corrupt
        # JSON blob). Surface as StorageError so operators can
        # distinguish "the runner sent bad input" from "the vault
        # row is broken" in logs and forensics.
        raise StorageError(f"corrupt automation row: {type(exc).__name__}") from exc
    except vault_automations.InvalidAutomationMetadataError as exc:
        raise ValidationError(str(exc)) from exc
    return RecordAutomationRunResponse(
        external_id=external_id,
        last_run=last_run,
        last_result=last_result,
    )


async def list_checks_for_agent(
    tctx: ToolContext,
    *,
    profile_external_id: str,
) -> ChecklistView | None:
    """MCP ``list_checks_for_agent`` — resolve an agent_profile's
    canonical verification_ref to the live checklist row.

    Read scope on ``verification_checklist`` is required. Returns
    ``None`` when the profile is missing, has no verification_ref,
    or the linked checklist is soft-deleted. Cross-agent reads are
    blocked by the storage-layer agent-id guard.
    """

    _validate_ulid(profile_external_id)
    _require_scope(tctx, op="read", facet_type="verification_checklist")
    checklist = vault_verification.get_canonical_for_profile(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        profile_external_id=profile_external_id,
    )
    if checklist is None:
        return None
    body = checklist.content
    body_tokens = count_tokens(body)
    truncated = False
    overhead = 256
    if body_tokens > LIST_CHECKS_FOR_AGENT_RESPONSE_BUDGET - overhead:
        body = truncate_snippet(body, max_tokens=LIST_CHECKS_FOR_AGENT_RESPONSE_BUDGET - overhead)
        truncated = True
        body_tokens = count_tokens(body)
    meta = checklist.metadata
    return ChecklistView(
        external_id=checklist.external_id,
        content=body,
        agent_ref=meta.agent_ref,
        trigger=meta.trigger,
        checks=tuple(
            ChecklistCheckView(id=c.id, statement=c.statement, severity=c.severity)
            for c in meta.checks
        ),
        pass_criteria=meta.pass_criteria,
        captured_at=checklist.captured_at,
        embed_status=checklist.embed_status,
        truncated=truncated,
        token_count=body_tokens,
    )


async def register_compiled_artifact(
    tctx: ToolContext,
    *,
    content: str,
    source_facets: Sequence[str],
    compiler_version: str,
    artifact_type: str = "playbook",
    metadata: dict[str, Any] | None = None,
    source_tool: str | None = None,
) -> RegisterCompiledArtifactResponse:
    """MCP ``register_compiled_artifact`` — store a compiled artifact.

    Write scope on ``compiled_notebook`` is required. The two-call
    API per ADR 0019 §Compilation pipeline: the caller-side
    compiler reads sources via ``list_compile_sources`` (or
    ``recall``), synthesises the narrative outside the daemon, and
    posts the rendered content here. Tessera stores; the caller
    compiles. There is no ``compile_now`` API by design.
    """

    _validate_length("content", content, _MAX_CONTENT_CHARS, allow_empty=False)
    _validate_compile_source_list(source_facets)
    _validate_length("compiler_version", compiler_version, 128, allow_empty=False)
    _validate_length("artifact_type", artifact_type, 64, allow_empty=False)
    _validate_metadata(metadata)
    resolved_source = source_tool or tctx.verified.client_name
    _validate_source_tool(resolved_source)
    _require_scope(tctx, op="write", facet_type="compiled_notebook")
    try:
        external_id = vault_compiled.register_compiled_artifact(
            tctx.conn,
            agent_id=tctx.verified.agent_id,
            content=content,
            source_facets=tuple(source_facets),
            artifact_type=artifact_type,
            compiler_version=compiler_version,
            source_tool=resolved_source,
            metadata=metadata,
        )
    except (
        vault_compiled.InvalidCompiledArtifactError,
        vault_compiled.DuplicateCompiledArtifactError,
    ) as exc:
        raise ValidationError(str(exc)) from exc
    except vault_facets.UnknownAgentError as exc:
        raise StorageError(f"agent resolution failed: {type(exc).__name__}") from exc
    return RegisterCompiledArtifactResponse(
        external_id=external_id,
        artifact_type=artifact_type,
        source_count=len(source_facets),
    )


async def get_compiled_artifact(
    tctx: ToolContext,
    *,
    external_id: str,
) -> CompiledArtifactView | None:
    """MCP ``get_compiled_artifact`` — fetch one artifact by external_id.

    Returns ``None`` when no artifact matches. Read scope on
    ``compiled_notebook`` is required. Cross-agent reads are
    blocked at the boundary by an explicit agent-id guard so a
    leaked ULID cannot be turned into a cross-agent read.
    """

    _validate_ulid(external_id)
    _require_scope(tctx, op="read", facet_type="compiled_notebook")
    artifact = vault_compiled.get(tctx.conn, external_id=external_id)
    if artifact is None:
        return None
    if artifact.agent_id != tctx.verified.agent_id:
        return None
    body = artifact.content
    body_tokens = count_tokens(body)
    truncated = False
    overhead = 256
    if body_tokens > GET_COMPILED_ARTIFACT_RESPONSE_BUDGET - overhead:
        body = truncate_snippet(body, max_tokens=GET_COMPILED_ARTIFACT_RESPONSE_BUDGET - overhead)
        truncated = True
        body_tokens = count_tokens(body)
    return CompiledArtifactView(
        external_id=artifact.external_id,
        content=body,
        artifact_type=artifact.artifact_type,
        source_facets=artifact.source_facets,
        compiler_version=artifact.compiler_version,
        compiled_at=artifact.compiled_at,
        is_stale=artifact.is_stale,
        truncated=truncated,
        token_count=body_tokens,
    )


async def list_compile_sources(
    tctx: ToolContext,
    *,
    target: str,
    limit: int = 50,
) -> ListCompileSourcesResponse:
    """MCP ``list_compile_sources`` — enumerate tagged source facets.

    Read scope on ``compiled_notebook`` is required so the caller
    holding write access can also pre-read its inputs without
    needing per-type read scopes for every source facet type.
    Returns rows whose ``metadata.compile_into`` array contains
    ``target``.
    """

    _validate_length("target", target, _MAX_COMPILE_TARGET_CHARS, allow_empty=False)
    _validate_limit(limit)
    _require_scope(tctx, op="read", facet_type="compiled_notebook")
    rows = vault_compiled.list_for_compilation(
        tctx.conn,
        agent_id=tctx.verified.agent_id,
        target=target,
        limit=limit,
    )
    items: list[CompileSourceView] = []
    for row in rows:
        snippet = truncate_snippet(row.content, max_tokens=512)
        items.append(
            CompileSourceView(
                external_id=row.external_id,
                facet_type=row.facet_type,
                snippet=snippet,
                captured_at=row.captured_at,
                token_count=count_tokens(snippet),
            )
        )
    trimmed, truncated, total_tokens = _budget_compile_sources(items)
    return ListCompileSourcesResponse(
        items=trimmed,
        truncated=truncated,
        total_tokens=total_tokens,
    )


# ---- Helpers -------------------------------------------------------------


def _validate_compile_source_list(source_facets: Sequence[str]) -> None:
    """Validate ``source_facets`` is a non-empty list of ULID strings.

    The storage layer enforces the same shape; this gate gives the
    MCP boundary a clean :class:`ValidationError` rather than
    surfacing the storage-layer exception verbatim.
    """

    if not isinstance(source_facets, list | tuple):
        raise ValidationError(f"source_facets must be a list, got {type(source_facets).__name__}")
    if not source_facets:
        raise ValidationError("source_facets must contain at least one entry")
    for index, entry in enumerate(source_facets):
        if not isinstance(entry, str) or not _ULID_PATTERN.match(entry):
            raise ValidationError(f"source_facets[{index}] must be a ULID string")


def _budget_compile_sources(
    items: list[CompileSourceView],
) -> tuple[tuple[CompileSourceView, ...], bool, int]:
    """Apply the list_compile_sources budget to source views."""

    per_row_overhead = 48
    budgeted: list[BudgetedItem] = [
        BudgetedItem(
            key=view.external_id,
            snippet=view.snippet,
            token_count=view.token_count + per_row_overhead,
        )
        for view in items
    ]
    trimmed = apply_budget(budgeted, total_budget=LIST_COMPILE_SOURCES_RESPONSE_BUDGET)
    kept_keys = {b.key for b in trimmed.items}
    kept = tuple(view for view in items if view.external_id in kept_keys)
    total = sum(item.token_count for item in trimmed.items)
    return kept, trimmed.truncated, total


def _enforce_same_agent_profile_ref(tctx: ToolContext, agent_ref: str) -> None:
    """Block cross-agent ``agent_ref`` writes at the MCP boundary.

    A token scoped to write checklists or retrospectives could
    otherwise plant a row pointing at another agent's
    ``agent_profile`` ULID. The storage layer would accept it
    because the row's own ``agent_id`` is set from the verified
    capability, not parsed from metadata. The scope check protects
    the *write target*; this guard protects the *referenced
    profile*. Together they keep an agent from polluting another
    agent's SWCR retrospective bundle.
    """

    profile = vault_agent_profiles.get(tctx.conn, external_id=agent_ref)
    if profile is None:
        raise ValidationError(f"agent_ref {agent_ref!r} does not resolve to a live agent_profile")
    if profile.agent_id != tctx.verified.agent_id:
        raise ValidationError(f"agent_ref {agent_ref!r} belongs to a different agent")


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


def _budget_agent_profile_summaries(
    summaries: list[AgentProfileSummary],
) -> tuple[tuple[AgentProfileSummary, ...], bool, int]:
    """Apply the list_agent_profiles budget to profile summaries.

    Each summary's token cost is the purpose snippet plus the cadence
    label, the skill-ref ULID list, and a per-row overhead for
    metadata. Truncation falls back to the trailing items so the
    most-recently-registered profile (the most likely active link)
    survives in the response.
    """

    items: list[BudgetedItem] = []
    for s in summaries:
        per_row_overhead = 48
        skill_ref_tokens = sum(count_tokens(ref) for ref in s.skill_refs)
        snippet = f"{s.purpose} | cadence={s.cadence}"
        items.append(
            BudgetedItem(
                key=s.external_id,
                snippet=snippet,
                token_count=(
                    count_tokens(s.purpose)
                    + count_tokens(s.cadence)
                    + skill_ref_tokens
                    + per_row_overhead
                ),
            )
        )
    trimmed = apply_budget(items, total_budget=LIST_AGENT_PROFILES_RESPONSE_BUDGET)
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
    "GET_AGENT_PROFILE_RESPONSE_BUDGET",
    "GET_COMPILED_ARTIFACT_RESPONSE_BUDGET",
    "GET_SKILL_RESPONSE_BUDGET",
    "LEARN_SKILL_RESPONSE_BUDGET",
    "LIST_AGENT_PROFILES_RESPONSE_BUDGET",
    "LIST_CHECKS_FOR_AGENT_RESPONSE_BUDGET",
    "LIST_COMPILE_SOURCES_RESPONSE_BUDGET",
    "LIST_FACETS_RESPONSE_BUDGET",
    "LIST_PEOPLE_RESPONSE_BUDGET",
    "LIST_SKILLS_RESPONSE_BUDGET",
    "MCP_TOOL_CONTRACTS",
    "RECALL_RESPONSE_BUDGET",
    "RECORD_RETROSPECTIVE_RESPONSE_BUDGET",
    "REGISTER_AGENT_PROFILE_RESPONSE_BUDGET",
    "REGISTER_CHECKLIST_RESPONSE_BUDGET",
    "REGISTER_COMPILED_ARTIFACT_RESPONSE_BUDGET",
    "RESOLVE_PERSON_RESPONSE_BUDGET",
    "SHOW_RESPONSE_BUDGET",
    "STATS_RESPONSE_BUDGET",
    "ActiveModel",
    "AgentProfileSummary",
    "AgentProfileView",
    "BudgetExceeded",
    "CaptureResponse",
    "ChecklistCheckView",
    "ChecklistView",
    "CompileSourceView",
    "CompiledArtifactView",
    "EmbedHealth",
    "FacetSummary",
    "ForgetResponse",
    "LearnSkillResponse",
    "ListAgentProfilesResponse",
    "ListCompileSourcesResponse",
    "ListFacetsResponse",
    "ListPeopleResponse",
    "ListSkillsResponse",
    "PersonMatch",
    "RecallMatchView",
    "RecallResponse",
    "RecordAutomationRunResponse",
    "RecordRetrospectiveResponse",
    "RegisterAgentProfileResponse",
    "RegisterAutomationResponse",
    "RegisterChecklistResponse",
    "RegisterCompiledArtifactResponse",
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
    "get_agent_profile",
    "get_compiled_artifact",
    "get_skill",
    "learn_skill",
    "list_agent_profiles",
    "list_checks_for_agent",
    "list_compile_sources",
    "list_facets",
    "list_people",
    "list_skills",
    "recall",
    "record_automation_run",
    "record_retrospective",
    "register_agent_profile",
    "register_automation",
    "register_checklist",
    "register_compiled_artifact",
    "resolve_person",
    "show",
    "stats",
]
