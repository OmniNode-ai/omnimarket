# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Budget wire types used by delegation request and compliance DTOs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBudgetAction(StrEnum):
    """Budget policy outcome used by the compliance loop."""

    CONTINUE = "CONTINUE"
    WARN = "WARN"
    THROTTLE = "THROTTLE"
    ABORT = "ABORT"


class ModelBudgetLimits(BaseModel):
    """Declared budget ceilings for a delegated task execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0.0)
    max_time_s: float = Field(gt=0.0)


__all__: list[str] = ["EnumBudgetAction", "ModelBudgetLimits"]
