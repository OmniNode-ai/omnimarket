# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_feature_dashboard_compute."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelFeatureDashboardResult(BaseModel):
    """Result of the feature dashboard skill connectivity audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_report: dict[str, object] = Field(
        default_factory=dict,
        description="Per-skill coverage map across all audited layers",
    )
    gaps: list[dict[str, object]] = Field(
        default_factory=list,
        description="Skills missing wiring in one or more layers",
    )
    status: Literal["complete", "partial", "empty"] = Field(
        default="empty",
        description="Overall audit status.",
    )
    skills_audited: int = Field(default=0, ge=0)
    checks_run: list[str] = Field(default_factory=list)


__all__ = ["ModelFeatureDashboardResult"]
