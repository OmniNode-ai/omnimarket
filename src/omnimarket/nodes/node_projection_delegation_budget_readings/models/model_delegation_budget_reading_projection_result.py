# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed output of the pure budget reading fold (OMN-21326 AC3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_delegation_budget_readings.models.model_delegation_budget_reading_row import (
    ModelDelegationBudgetReadingRow,
)


class ModelDelegationBudgetReadingProjectionResult(BaseModel):
    """The row to store, and whether it replaces what is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelDelegationBudgetReadingRow
    applied: bool


__all__ = ["ModelDelegationBudgetReadingProjectionResult"]
