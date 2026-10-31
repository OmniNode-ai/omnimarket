# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegate-skill terminal, as the budget readings projection reads it (OMN-21326 AC3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_delegation_budget_readings.models.model_delegation_budget_reading_budget import (
    ModelDelegationBudgetReadingBudget,
)


class ModelDelegationBudgetReadingEvent(BaseModel):
    """The fields of ``ModelDelegateSkillCompleted`` / ``Failed`` this table keeps.

    ``extra="ignore"``: the terminal carries the whole delegation answer, and
    this projection reads six of its fields. ``observed_at`` is not on the
    terminal's payload: the writer takes it from the producer's envelope time
    and refuses an event that has none, because the write time would date a
    run by when it was projected and not by when it ended.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID
    status: Literal["completed", "failed", "timeout"]
    task_type: str = Field(min_length=1)
    terminal_topic: str | None = None
    budget_evidence: ModelDelegationBudgetReadingBudget | None = None
    execution_duration_ms: int | None = Field(default=None, ge=0)
    queue_wait_ms: int | None = Field(default=None, ge=0)
    caller_lane: str | None = None
    ticket_id: str | None = None
    observed_at: datetime


__all__ = ["ModelDelegationBudgetReadingEvent"]
