# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-registered sample size of one task class and kind."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_tier_comparison.enum_comparison_kind import (
    EnumComparisonKind,
)


class ModelComparisonClassRequirement(BaseModel):
    """``n_required`` is the n the pre-registration fixed before the first call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    kind: EnumComparisonKind
    n_required: int = Field(ge=1)
