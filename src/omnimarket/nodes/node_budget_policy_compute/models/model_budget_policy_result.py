"""Result model for budget policy evaluation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumBudgetAction,
)


class ModelBudgetPolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumBudgetAction
    reason: str
    dimensions_exceeded: list[str]
    recommended_action: str


__all__ = ["ModelBudgetPolicyResult"]
