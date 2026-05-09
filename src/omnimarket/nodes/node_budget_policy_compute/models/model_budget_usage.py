"""Current resource consumption snapshot passed to the budget policy handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBudgetUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    elapsed_time_s: float = Field(ge=0.0)


__all__ = ["ModelBudgetUsage"]
