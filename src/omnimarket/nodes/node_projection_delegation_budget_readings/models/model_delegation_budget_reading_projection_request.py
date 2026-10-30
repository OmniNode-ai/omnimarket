# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input to the pure budget reading fold (OMN-21326 AC3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_delegation_budget_readings.models.model_delegation_budget_reading_event import (
    ModelDelegationBudgetReadingEvent,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.models.model_delegation_budget_reading_row import (
    ModelDelegationBudgetReadingRow,
)


class ModelDelegationBudgetReadingProjectionRequest(BaseModel):
    """The stored row for the event's correlation id, if any, and the event itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelDelegationBudgetReadingEvent
    previous_row: ModelDelegationBudgetReadingRow | None = None


__all__ = ["ModelDelegationBudgetReadingProjectionRequest"]
