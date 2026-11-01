# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of omninode_internal.delegation_budget_readings (OMN-21326 AC3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationBudgetReadingRow(BaseModel):
    """The latest terminal for one correlation id.

    NULL means the terminal did not say: a run with no ``budget_evidence`` has
    NULL timeouts, and a terminal that did not measure its handler has a NULL
    ``execution_duration_ms``. Neither is stored as zero. Whether a run outlived
    its budget plus grace is the readback's verdict and is not stored here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID
    status: Literal["completed", "failed", "timeout"]
    task_type: str = Field(min_length=1)
    terminal_topic: str | None = None
    requested_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_duration_ms: int | None = Field(default=None, ge=0)
    queue_wait_ms: int | None = Field(default=None, ge=0)
    caller_lane: str | None = None
    ticket_id: str | None = None
    observed_at: datetime


__all__ = ["ModelDelegationBudgetReadingRow"]
