# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The finding for one named check."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_freshness_alert_effect.models.enum_freshness_state import (
    EnumFreshnessState,
)


class ModelFreshnessVerdict(BaseModel):
    """Unobserved values remain None on UNKNOWN findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    check_id: str
    state: EnumFreshnessState
    supply_newest_at: datetime | None
    age_seconds: float | None
    demand_distinct_active: int | None
    error: str | None = None
