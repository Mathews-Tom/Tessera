"""Scope parsing, validation, and allow/deny matching."""

from __future__ import annotations

import pytest

from tessera.auth.scopes import (
    MalformedScopeError,
    Scope,
    UnknownFacetTypeError,
    build_scope,
    parse_scope,
)


@pytest.mark.unit
def test_build_scope_accepts_v0_1_facet_types() -> None:
    scope = build_scope(read=["style", "project"], write=["style"])
    assert scope.allows(op="read", facet_type="style")
    assert scope.allows(op="read", facet_type="project")
    assert scope.allows(op="write", facet_type="style")
    assert not scope.allows(op="write", facet_type="project")


@pytest.mark.unit
def test_build_scope_rejects_unknown_facet_type() -> None:
    with pytest.raises(UnknownFacetTypeError, match="spaghetti"):
        build_scope(read=["spaghetti"], write=[])


@pytest.mark.unit
def test_build_scope_allows_wildcard() -> None:
    scope = build_scope(read=["*"], write=["*"])
    assert scope.allows(op="read", facet_type="style")
    assert scope.allows(op="read", facet_type="person")
    assert scope.allows(op="write", facet_type="skill")


@pytest.mark.unit
def test_empty_scope_denies_everything() -> None:
    scope = build_scope(read=[], write=[])
    assert not scope.allows(op="read", facet_type="style")
    assert not scope.allows(op="write", facet_type="style")


@pytest.mark.unit
def test_to_json_round_trip_preserves_sets() -> None:
    original = build_scope(read=["style", "project"], write=["style"])
    restored = parse_scope(original.to_json())
    assert restored == original


@pytest.mark.unit
def test_to_json_is_canonical() -> None:
    # Sorted keys and sorted lists so two scopes with the same content
    # produce the same JSON string regardless of input order.
    a = build_scope(read=["project", "style"], write=[])
    b = build_scope(read=["style", "project"], write=[])
    assert a.to_json() == b.to_json()


@pytest.mark.unit
def test_parse_scope_rejects_non_object_root() -> None:
    with pytest.raises(MalformedScopeError, match="JSON object"):
        parse_scope('["style"]')


@pytest.mark.unit
def test_parse_scope_rejects_non_list_value() -> None:
    with pytest.raises(MalformedScopeError, match="must be a list"):
        parse_scope('{"read": "style", "write": []}')


@pytest.mark.unit
def test_parse_scope_rejects_malformed_json() -> None:
    with pytest.raises(MalformedScopeError, match="not valid JSON"):
        parse_scope('{"read": [')


@pytest.mark.unit
def test_parse_scope_rejects_non_string_entry() -> None:
    with pytest.raises(MalformedScopeError, match="must be strings"):
        parse_scope('{"read": [42], "write": []}')


@pytest.mark.unit
def test_parse_scope_rejects_unknown_facet() -> None:
    with pytest.raises(UnknownFacetTypeError, match="pasta"):
        parse_scope('{"read": ["pasta"], "write": []}')


@pytest.mark.unit
def test_allows_returns_false_for_unknown_facet_type_without_wildcard() -> None:
    # Unknown at query time — should deny rather than raise.
    scope = build_scope(read=["style"], write=[])
    assert not scope.allows(op="read", facet_type="not_a_facet")


@pytest.mark.unit
def test_scope_is_frozen() -> None:
    scope = Scope(read=frozenset({"style"}), write=frozenset())
    with pytest.raises(AttributeError):
        scope.read = frozenset({"project"})  # type: ignore[misc]
