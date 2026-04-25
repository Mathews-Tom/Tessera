"""``_DEFAULT_RECALL_TYPES`` invariants pinned for the v0.3 surface."""

from __future__ import annotations

import pytest

from tessera.daemon import dispatch
from tessera.vault import facets as vault_facets


@pytest.mark.unit
def test_default_recall_types_includes_skill_for_v0_3() -> None:
    """v0.3 lifts ``skill`` into the cross-facet recall default.

    Per docs/release-spec.md §v0.3 DoD: "recall includes top-K
    people and skills in cross-facet bundles when relevant". For
    skills (which are facets), this is achieved by adding ``skill``
    to ``_DEFAULT_RECALL_TYPES`` so a no-arg recall fans out over
    them alongside the original v0.1 types.
    """

    assert "skill" in dispatch._DEFAULT_RECALL_TYPES


@pytest.mark.unit
def test_default_recall_types_excludes_person() -> None:
    """``person`` is not a facet type — people live in their own table.

    The ``resolve_person`` MCP tool surfaces people by name; the
    cross-facet recall path operates on the ``facets`` table only.
    Including ``person`` in the default would hand the retrieval
    pipeline a facet_type with zero rows and produce no signal.
    """

    assert "person" not in dispatch._DEFAULT_RECALL_TYPES


@pytest.mark.unit
def test_default_recall_types_excludes_compiled_notebook() -> None:
    """``compiled_notebook`` is reserved for v0.5 write-time compilation.

    Until v0.5 activates the type, the default recall set must not
    include it — the write path rejects captures, and the read path
    has no rows to return. Adding it now would create asymmetry
    between the schema CHECK (which admits the type) and what the
    pipeline can usefully produce.
    """

    assert "compiled_notebook" not in dispatch._DEFAULT_RECALL_TYPES


@pytest.mark.unit
def test_default_recall_types_covers_every_v0_1_type() -> None:
    """Every v0.1 facet type stays in the default for backward-compat."""

    for facet_type in vault_facets.V0_1_FACET_TYPES:
        assert facet_type in dispatch._DEFAULT_RECALL_TYPES


@pytest.mark.unit
def test_default_recall_types_is_sorted() -> None:
    """Deterministic order so scope-filtered subsets stay stable."""

    assert list(dispatch._DEFAULT_RECALL_TYPES) == sorted(dispatch._DEFAULT_RECALL_TYPES)
