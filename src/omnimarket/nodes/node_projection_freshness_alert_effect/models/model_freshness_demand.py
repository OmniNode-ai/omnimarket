# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The independent activity signal required to alert."""

from pydantic import ConfigDict, Field

from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_supply import (
    ModelFreshnessSupply,
)


class ModelFreshnessDemand(ModelFreshnessSupply):
    """Demand activity within a contract-declared trailing window."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    distinct_column: str = Field(
        ..., pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$"
    )
    window_seconds: int = Field(..., gt=0, strict=True)
    min_distinct: int = Field(..., gt=0, strict=True)
