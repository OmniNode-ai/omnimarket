"""Declared budget ceilings for a task execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBudgetLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0.0)
    max_time_s: float = Field(gt=0.0)


__all__ = ["ModelBudgetLimits"]
