# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_feature_dashboard_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFeatureDashboardRequest(BaseModel):
    """Request to audit skill connectivity across platform layers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skills: list[str] | None = Field(
        default=None,
        description="Optional skill name filter; null means all skills",
    )
    check_types: list[str] | None = Field(
        default=None,
        description="Layer check types to run; null means all 8 layers",
    )
