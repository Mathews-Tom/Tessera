"""Structured capability scopes per ADR 0007 / release-spec.md §Scopes.

A scope is a pair of facet-type allowlists — one for reads, one for
writes — stored as JSON text in the ``capabilities.scopes`` column. The
allowlists are closed: entries must match either the post-reframe
facet-type vocabulary (``docs/adr/0010-five-facet-user-context-model.md``)
or the wildcard ``"*"``. An empty list means "no access in this
direction", which is different from "wildcard".

The allowlist contains every facet type reserved by the schema CHECK —
the five writable v0.1 types plus the v0.3 (``person``, ``skill``) and
v0.5 (``compiled_notebook``, ``agent_profile``,
``verification_checklist``, ``retrospective``, ``automation``)
reservations. A token may be scoped to read a reserved type today; the
write surface separately rejects captures for types not yet activated.
This lets tokens issued at v0.1 keep working after later versions
activate their facets without a rotation.

The separation between ``read`` and ``write`` maps directly onto the
MCP tool surface: ``capture`` consults ``write``; ``recall``, ``show``,
``list_facets``, ``forget`` consult ``read`` (``forget`` additionally
consults ``write`` for the target facet's type). Admin-only ops
(``stats``) are gated by client class, not by facet scope.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

# Allowlist of facet types accepted in stored scopes. Covers the five
# v0.1 types plus the reserved v0.3/v0.5 types so tokens granted under
# v0.1 can carry a read scope that survives into later versions without
# a rotation. Kept local to this module rather than imported from
# ``tessera.vault.facets`` so the auth layer has no reverse dependency
# on the storage layer — scope validation must work even when the
# capabilities row is being constructed before any facet exists.
_ALLOWED_FACET_TYPES: Final[frozenset[str]] = frozenset(
    {
        "identity",
        "preference",
        "workflow",
        "project",
        "style",
        "person",
        "skill",
        "compiled_notebook",
        "agent_profile",
        "verification_checklist",
        "retrospective",
        "automation",
    }
)

ScopeOp = Literal["read", "write"]
_WILDCARD: Final[str] = "*"


class ScopeError(Exception):
    """Base class for scope-parsing failures."""


class MalformedScopeError(ScopeError):
    """Stored or supplied scope JSON does not match the expected shape."""


class UnknownFacetTypeError(ScopeError):
    """Scope references a facet type outside the ADR-0010 vocabulary.

    The allowlist is the five v0.1 writable types plus the v0.3
    (``person``, ``skill``) and v0.5 (``compiled_notebook``)
    reservations. An entry outside that set raises; whether the target
    type is *active* (writable at the current version) is a separate
    concern enforced at the capture boundary.
    """


@dataclass(frozen=True, slots=True)
class Scope:
    """An immutable read/write capability grant.

    Wildcards are normalised away at parse time: a scope of ``["*"]`` is
    preserved as ``frozenset({"*"})`` so :meth:`allows` can do one-shot
    wildcard check without scanning the set.
    """

    read: frozenset[str]
    write: frozenset[str]

    def allows(self, *, op: ScopeOp, facet_type: str) -> bool:
        """Return True iff this scope grants ``op`` on ``facet_type``.

        Facet-type validation is the caller's responsibility: this method
        deliberately returns False for unknown facet types rather than
        raising, so the MCP boundary can surface a single ``scope_denied``
        error path regardless of whether the denial was vocabulary or
        policy.
        """

        allowlist = self.read if op == "read" else self.write
        if _WILDCARD in allowlist:
            return True
        return facet_type in allowlist

    def to_json(self) -> str:
        """Serialise to the canonical JSON shape stored in the vault."""

        return json.dumps(
            {"read": sorted(self.read), "write": sorted(self.write)},
            sort_keys=True,
            ensure_ascii=False,
        )


def parse_scope(raw: str) -> Scope:
    """Parse the JSON string stored in ``capabilities.scopes``.

    Raises :class:`MalformedScopeError` for structural problems and
    :class:`UnknownFacetTypeError` when a non-wildcard entry references
    an unknown facet type. Both errors are terminal: a malformed stored
    scope is not recoverable and should surface as ``auth_denied`` at the
    MCP boundary.
    """

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedScopeError(f"scopes column is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedScopeError(f"scopes must be a JSON object, got {type(obj).__name__}")
    read = _parse_list(obj, key="read")
    write = _parse_list(obj, key="write")
    return Scope(read=read, write=write)


def build_scope(*, read: Sequence[str], write: Sequence[str]) -> Scope:
    """Build a Scope from user-supplied lists with validation.

    Caller-facing constructor for CLI / token issue paths. Raises the
    same errors as :func:`parse_scope` so the two entry points share one
    validation surface.
    """

    return Scope(
        read=_normalise(read, key="read"),
        write=_normalise(write, key="write"),
    )


def _parse_list(obj: dict[str, object], *, key: str) -> frozenset[str]:
    val = obj.get(key, [])
    if not isinstance(val, list):
        raise MalformedScopeError(f"scopes['{key}'] must be a list, got {type(val).__name__}")
    return _normalise(val, key=key)


def _normalise(items: Sequence[object], *, key: str) -> frozenset[str]:
    out: set[str] = set()
    for entry in items:
        if not isinstance(entry, str):
            raise MalformedScopeError(
                f"scopes['{key}'] entries must be strings, got {type(entry).__name__}"
            )
        if entry == _WILDCARD:
            out.add(entry)
            continue
        if entry not in _ALLOWED_FACET_TYPES:
            raise UnknownFacetTypeError(f"scopes['{key}'] references unknown facet type {entry!r}")
        out.add(entry)
    return frozenset(out)


__all__ = [
    "MalformedScopeError",
    "Scope",
    "ScopeError",
    "ScopeOp",
    "UnknownFacetTypeError",
    "build_scope",
    "parse_scope",
]
