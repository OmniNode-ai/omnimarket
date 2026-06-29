"""Result model for budget policy evaluation."""

from __future__ import annotations

from omnibase_core.enums.enum_budget_action import EnumBudgetAction
from pydantic import BaseModel, ConfigDict


class ModelBudgetPolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumBudgetAction
    reason: str
    dimensions_exceeded: list[str]
    recommended_action: str


__all__ = ["ModelBudgetPolicyResult"]
