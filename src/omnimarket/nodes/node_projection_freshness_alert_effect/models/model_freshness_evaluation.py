# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed terminal event returned from the def-B handler."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_delivery import (
    ModelFreshnessDelivery,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models.model_freshness_verdict import (
    ModelFreshnessVerdict,
)


class ModelFreshnessEvaluation(BaseModel):
    """Every finding and every attempted Slack publication from this tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    evaluated: bool
    check_interval_seconds: int = Field(..., gt=0)
    verdicts: tuple[ModelFreshnessVerdict, ...]
    deliveries: tuple[ModelFreshnessDelivery, ...]
