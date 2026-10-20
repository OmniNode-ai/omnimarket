# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Activity context and an optional minimum required to alert."""

from pydantic import ConfigDict, Field

from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_supply import (
    ModelFreshnessSupply,
)


class ModelFreshnessDemand(ModelFreshnessSupply):
    """Demand in a trailing window; zero minimum keeps the alarm always active."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    distinct_column: str = Field(
        ..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$"
    )
    window_seconds: int = Field(..., gt=0, strict=True)
    min_distinct: int = Field(..., ge=0, strict=True)
