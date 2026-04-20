"""Role map for ``assume_identity`` per docs/swcr-spec.md §Per-type budget.

An identity bundle is a curated collection, not a flat top-K. Each role
answers a distinct question the fresh substrate needs answered:

- ``voice``          — "how does this agent write?"        → style
- ``recent_events``  — "what's been happening lately?"     → episodic
- ``skills``         — "what procedures does it know?"     → skill   (v0.3)
- ``relationships``  — "who does it work with?"            → relationship (v0.5)
- ``goals``          — "what is it trying to do?"          → goal    (v0.5)

Each role ships a ``budget_fraction`` of the bundle's token budget, plus
a ``(k_min, k_max)`` pair that caps how many facets of that type can
land in the result. ``k_min`` is the floor: if the budget can afford it,
the assembler prefers to return at least ``k_min`` of each active role so
a fresh substrate never opens with zero samples of voice or zero recent
context. ``k_max`` is the ceiling: above it, the role starves the others.

At v0.1 only two roles are active (style + episodic). The other three
facet types do not exist yet; the role map still lists them so the
role-specification work is done once and the v0.3+ graduation is
additive. ``active_roles_for_schema()`` filters the default list down
to the roles whose facet_type is supported by the live vault schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tessera.vault.facets import V0_1_FACET_TYPES

BUNDLE_DEFAULT_BUDGET_TOKENS: Final[int] = 6000
BUNDLE_DEFAULT_WINDOW_HOURS: Final[int] = 168


@dataclass(frozen=True, slots=True)
class RoleSpec:
    name: str
    facet_type: str
    budget_fraction: float
    k_min: int
    k_max: int
    time_window_hours: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.budget_fraction <= 1.0:
            raise ValueError(
                f"role {self.name!r}: budget_fraction must be in (0, 1]; got {self.budget_fraction}"
            )
        if self.k_min < 0:
            raise ValueError(f"role {self.name!r}: k_min must be >= 0; got {self.k_min}")
        if self.k_max < self.k_min:
            raise ValueError(
                f"role {self.name!r}: k_max must be >= k_min; got {self.k_min}, {self.k_max}"
            )


DEFAULT_ROLES: Final[tuple[RoleSpec, ...]] = (
    RoleSpec(
        name="voice",
        facet_type="style",
        budget_fraction=0.25,
        k_min=3,
        k_max=8,
    ),
    RoleSpec(
        name="recent_events",
        facet_type="episodic",
        budget_fraction=0.30,
        k_min=5,
        k_max=15,
        time_window_hours=BUNDLE_DEFAULT_WINDOW_HOURS,
    ),
    # v0.3+ (skill) and v0.5+ (relationship, goal). Disabled until the
    # facet types exist in the vault schema. Listed here so the v0.3+
    # graduation edits one place and the bundle assembler keeps the role
    # order stable.
    RoleSpec(
        name="skills",
        facet_type="skill",
        budget_fraction=0.20,
        k_min=2,
        k_max=6,
    ),
    RoleSpec(
        name="relationships",
        facet_type="relationship",
        budget_fraction=0.15,
        k_min=2,
        k_max=5,
    ),
    RoleSpec(
        name="goals",
        facet_type="goal",
        budget_fraction=0.10,
        k_min=1,
        k_max=3,
    ),
)


def active_roles_for_schema(
    roles: tuple[RoleSpec, ...] = DEFAULT_ROLES,
    *,
    supported_facet_types: frozenset[str] = V0_1_FACET_TYPES,
) -> tuple[RoleSpec, ...]:
    """Filter a role list down to the roles whose facet_type is live.

    Unused roles are dropped rather than silently populated with empty
    results — the bundle's per-role output tuple must not carry a key
    whose facet type does not exist in the vault.
    """

    return tuple(r for r in roles if r.facet_type in supported_facet_types)


def normalise_budget_fractions(roles: tuple[RoleSpec, ...]) -> dict[str, float]:
    """Return a ``{role_name: fraction}`` that sums to 1.0.

    When some roles are inactive (v0.1 case: skills/relationships/goals
    absent), the original fractions sum to less than 1.0 and leave budget
    unused. Normalising proportionally keeps the relative weight between
    active roles unchanged while filling the bundle budget. Voice:recent
    in v0.1 is 0.25 : 0.30; after normalisation 0.45 : 0.55.
    """

    total = sum(r.budget_fraction for r in roles)
    if total <= 0.0:
        raise ValueError("cannot normalise an empty role list")
    return {r.name: r.budget_fraction / total for r in roles}
