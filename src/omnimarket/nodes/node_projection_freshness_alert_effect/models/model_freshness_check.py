# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One generic contract-declared freshness check."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_demand import (
    ModelFreshnessDemand,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_supply import (
    ModelFreshnessSupply,
)


class ModelFreshnessCheck(BaseModel):
    """Supply and independent demand, with required alert thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    check_id: str = Field(..., pattern=r"^[a-z_][a-z0-9_]*$")
    max_age_seconds: int = Field(..., gt=0, strict=True)
    supply: ModelFreshnessSupply
    demand: ModelFreshnessDemand
