"""Pure-Python validators on the MCP tool surface.

The tool primitives themselves hit the vault, so their full exercise
lives in ``tests/integration/test_mcp_tool_surface.py``. This module
pins the boundary-level input rules so a bad payload fails at validate
time, before any storage or retrieval work has begun.
"""

from __future__ import annotations

import pytest

from tessera.mcp_surface.tools import (
    _SOURCE_TOOL_PATTERN,
    _ULID_PATTERN,
    ValidationError,
    _resolve_response_budget,
    _validate_facet_type,
    _validate_k,
    _validate_length,
    _validate_limit,
    _validate_metadata,
    _validate_since,
    _validate_source_tool,
    _validate_ulid,
)


@pytest.mark.unit
def test_validate_length_rejects_empty_when_disallowed() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        _validate_length("content", "", max_chars=64, allow_empty=False)


@pytest.mark.unit
def test_validate_length_rejects_overflow() -> None:
    too_long = "x" * 65
    with pytest.raises(ValidationError, match="exceeds max 64"):
        _validate_length("content", too_long, max_chars=64, allow_empty=False)


@pytest.mark.unit
def test_validate_length_rejects_non_string() -> None:
    with pytest.raises(ValidationError, match="must be a string"):
        _validate_length("content", 42, max_chars=64, allow_empty=False)  # type: ignore[arg-type]


@pytest.mark.unit
def test_validate_facet_type_accepts_active_v0_5_vocab() -> None:
    # V0.5-P2 unlocked ``agent_profile``; V0.5-P3 unlocked
    # ``verification_checklist`` and ``retrospective``; V0.5-P4
    # unlocks ``compiled_notebook`` (the AgenticOS Playbook). The
    # MCP boundary must accept every writable type.
    for t in (
        "identity",
        "preference",
        "workflow",
        "project",
        "style",
        "person",
        "skill",
        "agent_profile",
        "verification_checklist",
        "retrospective",
        "compiled_notebook",
    ):
        _validate_facet_type(t)


@pytest.mark.unit
def test_validate_facet_type_rejects_automation_until_v0_5_p5() -> None:
    # ``automation`` is the remaining v0.5 reserved type that stays
    # CHECK-permitted but write-rejected until V0.5-P5 ships the
    # storage-only registry.
    with pytest.raises(ValidationError, match="not in"):
        _validate_facet_type("automation")


@pytest.mark.unit
@pytest.mark.parametrize("retired", ["episodic", "semantic", "relationship", "goal", "judgment"])
def test_validate_facet_type_rejects_retired_types(retired: str) -> None:
    # Per ADR 0010 the retired facet types are no longer writable; the
    # MCP boundary must reject them so stale clients can't sneak rows
    # past the schema CHECK.
    with pytest.raises(ValidationError, match="not in"):
        _validate_facet_type(retired)


@pytest.mark.unit
def test_validate_facet_type_rejects_unknown() -> None:
    with pytest.raises(ValidationError, match="not in"):
        _validate_facet_type("hallucination")


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "claude-desktop",
        "Claude_Desktop",
        "cli",
        "a",  # single-char ok
        "svc.v2-2026",
    ],
)
def test_source_tool_pattern_accepts(name: str) -> None:
    _validate_source_tool(name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading-dash",
        "a" * 65,  # too long
        "has spaces",
        "unicode-ümlaut",
        "slash/sep",
    ],
)
def test_source_tool_pattern_rejects(name: str) -> None:
    with pytest.raises(ValidationError):
        _validate_source_tool(name)


@pytest.mark.unit
def test_validate_k_accepts_bounds() -> None:
    _validate_k(1)
    _validate_k(100)


@pytest.mark.unit
@pytest.mark.parametrize("k", [0, -1, 101, 10_000])
def test_validate_k_rejects_out_of_range(k: int) -> None:
    with pytest.raises(ValidationError, match=r"outside \[1, 100\]"):
        _validate_k(k)


@pytest.mark.unit
def test_validate_k_rejects_non_int() -> None:
    with pytest.raises(ValidationError, match="must be an integer"):
        _validate_k("10")  # type: ignore[arg-type]
    # bool is a subclass of int; explicitly reject it so True doesn't
    # sneak past as k=1.
    with pytest.raises(ValidationError):
        _validate_k(True)


@pytest.mark.unit
def test_validate_limit_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _validate_limit(0)
    with pytest.raises(ValidationError):
        _validate_limit(101)


@pytest.mark.unit
def test_validate_ulid_accepts_canonical_shape() -> None:
    # Canonical example from the ULID spec.
    _validate_ulid("01ARZ3NDEKTSV4RRFFQ69G5FAV")


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-ulid",
        "01ARZ3NDEKTSV4RRFFQ69G5FA",  # 25 chars, too short
        "01arz3ndektsv4rrffq69g5fav",  # lowercase rejected
        "01ARZ3NDEKTSV4RRFFQ69G5FAI",  # 'I' not in Crockford alphabet
        "01ARZ3NDEKTSV4RRFFQ69G5FAU",  # 'U' not in Crockford alphabet
    ],
)
def test_validate_ulid_rejects_bad_shape(value: str) -> None:
    with pytest.raises(ValidationError):
        _validate_ulid(value)


@pytest.mark.unit
def test_resolve_response_budget_clamps_to_ceiling() -> None:
    assert _resolve_response_budget(10_000, 6_000) == 6_000
    assert _resolve_response_budget(3_000, 6_000) == 3_000
    assert _resolve_response_budget(None, 6_000) == 6_000


@pytest.mark.unit
def test_resolve_response_budget_rejects_zero_and_negative() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        _resolve_response_budget(0, 6_000)
    with pytest.raises(ValidationError):
        _resolve_response_budget(-1, 6_000)


@pytest.mark.unit
def test_validate_since_accepts_bounds() -> None:
    _validate_since(0)
    _validate_since(253_402_300_799)


@pytest.mark.unit
@pytest.mark.parametrize("since", [-1, 253_402_300_800, 10**20])
def test_validate_since_rejects_out_of_range(since: int) -> None:
    with pytest.raises(ValidationError):
        _validate_since(since)


@pytest.mark.unit
def test_validate_metadata_accepts_none_and_small_dict() -> None:
    _validate_metadata(None)
    _validate_metadata({"source": "cli", "mode": "test"})


@pytest.mark.unit
def test_validate_metadata_rejects_too_many_keys() -> None:
    payload = {f"k{i}": i for i in range(40)}
    with pytest.raises(ValidationError, match="keys"):
        _validate_metadata(payload)


@pytest.mark.unit
def test_validate_metadata_rejects_non_string_key() -> None:
    with pytest.raises(ValidationError, match="keys must be strings"):
        _validate_metadata({1: "value"})  # type: ignore[dict-item]


@pytest.mark.unit
def test_validate_metadata_rejects_oversized_payload() -> None:
    big_value = "x" * 5_000
    with pytest.raises(ValidationError, match="serialised size"):
        _validate_metadata({"big": big_value})


@pytest.mark.unit
def test_validate_metadata_rejects_non_dict() -> None:
    with pytest.raises(ValidationError, match="must be a dict"):
        _validate_metadata([("a", 1)])  # type: ignore[arg-type]


@pytest.mark.unit
def test_regex_patterns_are_anchored() -> None:
    # Regressions where the anchors were dropped would let
    # "good_prefix_then bad chars" slip past. Anchor sanity check.
    assert _SOURCE_TOOL_PATTERN.pattern.startswith("^")
    assert _SOURCE_TOOL_PATTERN.pattern.endswith("$")
    assert _ULID_PATTERN.pattern.startswith("^")
    assert _ULID_PATTERN.pattern.endswith("$")
