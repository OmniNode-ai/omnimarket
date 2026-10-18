# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated database aggregates, before classification."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelFreshnessObservation(BaseModel):
    """Missing supply is meaningful; missing demand is an unusable read."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )
    supply_newest_at: AwareDatetime | None
    demand_distinct_active: int = Field(..., ge=0)
