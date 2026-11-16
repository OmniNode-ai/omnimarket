# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One declared tier of a context profile: its name, the factor it is carried as, its mode and its own token budget."""

from __future__ import annotations

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


__all__ = ["LANE_BRIEF_TIER_FACTORS", "ModelContextTierSpec"]
