# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The judged arms of one eval run and the pre-registered rule they are measured against."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_tier_comparison.model_comparison_arm import (
    ModelComparisonArm,
)
from omnimarket.models.delegation_tier_comparison.model_comparison_class_requirement import (
    ModelComparisonClassRequirement,
)


class ModelComparisonRequest(BaseModel):
    """Defaults are the pre-registered pass rule of the plan's section 2.4."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: str = Field(min_length=1)
    prereg_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    measured_at: date
    requirements: tuple[ModelComparisonClassRequirement, ...]
    arms: tuple[ModelComparisonArm, ...]
    accept_rate_min: float = Field(default=0.80, ge=0.0, le=1.0)
    wilson_low_min: float = Field(default=0.60, ge=0.0, le=1.0)
    gap_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    bootstrap_resamples: int = Field(default=10_000, ge=100)
    bootstrap_seed: int = 20261011
