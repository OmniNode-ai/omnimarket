# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One arm of one task class: its identity and its judged items."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_tier_comparison.enum_comparison_arm_kind import (
    EnumComparisonArmKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_cost_basis import (
    EnumComparisonCostBasis,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_kind import (
    EnumComparisonKind,
)
from omnimarket.models.delegation_tier_comparison.enum_comparison_tier import (
    EnumComparisonTier,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_observation import (
    ModelComparisonObservation,
)


class ModelComparisonArm(BaseModel):
    """A Claude arm names its tier; a local arm names its host and no tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    kind: EnumComparisonKind
    mapped_tier: EnumComparisonTier
    arm_model_key: str = Field(min_length=1)
    arm_kind: EnumComparisonArmKind
    claude_tier: EnumComparisonTier | None = None
    arm_host: str | None = Field(default=None, min_length=1)
    cost_basis: EnumComparisonCostBasis
    judge_flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    observations: tuple[ModelComparisonObservation, ...] = ()

    @model_validator(mode="after")
    def _check_arm_shape(self) -> Self:
        claude = self.arm_kind is EnumComparisonArmKind.CLAUDE
        if claude != (self.claude_tier is not None):
            raise ValueError("a claude arm names claude_tier and a local arm does not")
        if claude != (self.cost_basis is EnumComparisonCostBasis.LIST):
            raise ValueError("cost_basis list belongs to claude arms alone")
        if claude and self.arm_host is not None:
            raise ValueError("arm_host is for local arms only")
        return self
