# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The part of a terminal's budget_evidence the readback measures (OMN-21326 AC3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationBudgetReadingBudget(BaseModel):
    """What the run resolved before dispatch.

    Read from the terminal's ``budget_evidence`` with ``extra="ignore"``: the
    producer owns the full model (omnibase_core), and a field it adds must not
    dead-letter a projection that does not read it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    requested_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_timeout_seconds: int | None = Field(default=None, ge=1)


__all__ = ["ModelDelegationBudgetReadingBudget"]
