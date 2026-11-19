# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One declared tier of a context profile: its name, the factor it is carried as, its mode and its own token budget."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_lane_brief_tier import (
    MODEL_SELECTED_TIERS,
    EnumLaneBriefTier,
    EnumLaneBriefTierMode,
)

#: The factor each lane-brief tier is carried as: the factor enum belongs to the core package, so a tier
#: with no factor of its own rides the nearest existing one.
LANE_BRIEF_TIER_FACTORS: dict[EnumLaneBriefTier, EnumContextFactor] = {
    EnumLaneBriefTier.EXEMPLAR: EnumContextFactor.EXEMPLAR,
    EnumLaneBriefTier.GRAPH_ANSWERS: EnumContextFactor.ARCHITECTURE_PATTERNS,
    EnumLaneBriefTier.REVIEW_FINDINGS: EnumContextFactor.LOCAL_FAILURES,
    EnumLaneBriefTier.FILES_TO_TOUCH: EnumContextFactor.REPO_STATE,
    EnumLaneBriefTier.TESTS_AND_CI: EnumContextFactor.GOLDEN_CHAIN,
    EnumLaneBriefTier.LANE_HISTORY: EnumContextFactor.ORCHESTRATION_STATE,
    EnumLaneBriefTier.RISKS: EnumContextFactor.INVARIANTS,
}


class ModelContextTierSpec(BaseModel):
    """A tier's budget is its own: the profile's ``token_budget`` bounds the sum, never one tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: EnumLaneBriefTier
    factor: EnumContextFactor
    mode: EnumLaneBriefTierMode
    token_budget: int = Field(gt=0)

    @model_validator(mode="after")
    def _tier_is_carried_as_its_own_factor_and_mode(self) -> ModelContextTierSpec:
        if self.factor is not LANE_BRIEF_TIER_FACTORS[self.tier]:
            raise ValueError(
                f"tier {self.tier.value} is carried as factor "
                f"{LANE_BRIEF_TIER_FACTORS[self.tier].value}, not {self.factor.value}"
            )
        model_selected = self.tier in MODEL_SELECTED_TIERS
        if model_selected != (self.mode is EnumLaneBriefTierMode.MODEL_SELECTED):
            raise ValueError(
                f"tier {self.tier.value} is "
                f"{'model_selected' if model_selected else 'deterministic'}: "
                f"a deterministic tier is read by code and never calls a model"
            )
        return self


def check_tier_specs_in_order(specs: Sequence[ModelContextTierSpec]) -> None:
    """Refuse tier specs that do not name each lane-brief tier once, in pack order (the order the pack is built in)."""
    named = [spec.tier for spec in specs]
    missing = [t.value for t in EnumLaneBriefTier if t not in named]
    if missing or len(set(named)) != len(named):
        raise ValueError(
            f"tier_specs must name each lane-brief tier exactly once; missing: {missing}"
        )
    if named != list(EnumLaneBriefTier):
        raise ValueError(
            "tier_specs are out of order; the pack order is "
            + ", ".join(t.value for t in EnumLaneBriefTier)
        )


class _ProfileTierSpecs(Protocol):
    """The profile fields the check reads; the profile model satisfies it without being imported here."""

    @property
    def tier_specs(self) -> Sequence[ModelContextTierSpec]: ...

    @property
    def required_factors(self) -> Sequence[EnumContextFactor]: ...

    @property
    def token_budget(self) -> int: ...


def check_profile_tier_specs(profile: _ProfileTierSpecs) -> None:
    """Refuse a profile whose ``tier_specs`` are out of order, outside its required factors or above its budget."""
    specs = profile.tier_specs
    if not specs:
        return
    check_tier_specs_in_order(specs)
    if not {spec.factor for spec in specs} <= set(profile.required_factors):
        raise ValueError("every tier_specs factor must be in required_factors")
    total = sum(spec.token_budget for spec in specs)
    if total > profile.token_budget:
        raise ValueError(
            f"tier_specs budgets sum to {total}, above token_budget {profile.token_budget}"
        )


__all__ = [
    "LANE_BRIEF_TIER_FACTORS",
    "ModelContextTierSpec",
    "check_profile_tier_specs",
    "check_tier_specs_in_order",
]
