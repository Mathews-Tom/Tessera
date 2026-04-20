"""Role map: normalisation, schema filtering, validation."""

from __future__ import annotations

import pytest

from tessera.identity.roles import (
    DEFAULT_ROLES,
    RoleSpec,
    active_roles_for_schema,
    normalise_budget_fractions,
)


@pytest.mark.unit
def test_default_roles_match_spec_shape() -> None:
    names = [r.name for r in DEFAULT_ROLES]
    assert names == ["voice", "recent_events", "skills", "relationships", "goals"]
    # Total budget fraction across all five is 1.0 per spec §Per-type budget.
    total = sum(r.budget_fraction for r in DEFAULT_ROLES)
    assert total == pytest.approx(1.0)


@pytest.mark.unit
def test_active_roles_for_schema_drops_unsupported_types() -> None:
    active = active_roles_for_schema()
    assert [r.name for r in active] == ["voice", "recent_events"]
    # The dropped roles' facet_types do not appear in the v0.1 schema.
    for role in active:
        assert role.facet_type in {"style", "episodic"}


@pytest.mark.unit
def test_active_roles_with_custom_schema_keeps_matching_roles() -> None:
    active = active_roles_for_schema(
        supported_facet_types=frozenset({"style", "episodic", "skill"})
    )
    assert [r.name for r in active] == ["voice", "recent_events", "skills"]


@pytest.mark.unit
def test_normalise_budget_fractions_sums_to_one_for_v0_1() -> None:
    active = active_roles_for_schema()
    fractions = normalise_budget_fractions(active)
    assert sum(fractions.values()) == pytest.approx(1.0)
    # v0.1 split: voice 0.25 / total 0.55 ~= 0.4545; recent 0.30 / 0.55 ~= 0.5455.
    assert fractions["voice"] == pytest.approx(0.25 / 0.55)
    assert fractions["recent_events"] == pytest.approx(0.30 / 0.55)


@pytest.mark.unit
def test_normalise_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="empty role list"):
        normalise_budget_fractions(())


@pytest.mark.unit
def test_rolespec_rejects_out_of_range_budget() -> None:
    with pytest.raises(ValueError, match="budget_fraction"):
        RoleSpec(
            name="bad",
            facet_type="style",
            budget_fraction=0.0,
            k_min=1,
            k_max=3,
        )
    with pytest.raises(ValueError, match="budget_fraction"):
        RoleSpec(
            name="bad",
            facet_type="style",
            budget_fraction=1.5,
            k_min=1,
            k_max=3,
        )


@pytest.mark.unit
def test_rolespec_rejects_k_min_above_k_max() -> None:
    with pytest.raises(ValueError, match="k_max must be >= k_min"):
        RoleSpec(
            name="bad",
            facet_type="style",
            budget_fraction=0.5,
            k_min=10,
            k_max=5,
        )


@pytest.mark.unit
def test_rolespec_rejects_negative_k_min() -> None:
    with pytest.raises(ValueError, match="k_min must be >= 0"):
        RoleSpec(
            name="bad",
            facet_type="style",
            budget_fraction=0.5,
            k_min=-1,
            k_max=3,
        )


@pytest.mark.unit
def test_recent_events_has_time_window_default() -> None:
    recent = next(r for r in DEFAULT_ROLES if r.name == "recent_events")
    assert recent.time_window_hours == 168  # 7 days


@pytest.mark.unit
def test_non_time_windowed_roles_have_none() -> None:
    for role in DEFAULT_ROLES:
        if role.name == "recent_events":
            continue
        assert role.time_window_hours is None
