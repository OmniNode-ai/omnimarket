# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event of one heartbeat's evaluation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_work_progress_alert_effect.models.enum_work_progress_outcome import (
    EnumWorkProgressOutcome,
)
from omnimarket.nodes.node_work_progress_alert_effect.models.model_work_progress_delivery import (
    ModelWorkProgressDelivery,
)


class ModelWorkProgressEvaluated(BaseModel):
    """Counts when evaluated; the reason when refused or unreadable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumWorkProgressOutcome
    reason: str | None = None
    check_interval_seconds: int | None = Field(default=None, gt=0)
    subjects: int = 0
    findings: int = 0
    commands: int = 0
    deliveries: tuple[ModelWorkProgressDelivery, ...] = ()
