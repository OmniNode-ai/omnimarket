# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An arm below its pre-registered sample size."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelComparisonUnderpowered(BaseModel):
    """Reported, never counted as a pass (the plan's section 2.4, rule 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    arm_model_key: str = Field(min_length=1)
    n: int = Field(ge=1)
    n_required: int = Field(ge=1)
