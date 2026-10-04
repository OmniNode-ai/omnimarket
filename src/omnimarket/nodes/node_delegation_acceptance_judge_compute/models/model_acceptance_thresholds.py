# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run thresholds: a run that misses any of them is FAILED."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceThresholds(BaseModel):
    """Run thresholds: a run that misses any of them is FAILED."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kappa_min: float = Field(ge=-1.0, le=1.0)
    double_judge_fraction: float = Field(gt=0.0, le=1.0)
    double_judge_min_per_cell: int = Field(ge=1)
    min_judged_per_cell: int = Field(ge=1)
    accept_min_quality: int = Field(ge=0, le=3)
    reason_max_chars: int = Field(ge=1)
