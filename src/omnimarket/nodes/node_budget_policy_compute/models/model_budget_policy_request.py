"""Request model for budget policy evaluation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumTaskPriority,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_usage import (
    ModelBudgetUsage,
)


class ModelBudgetPolicyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_usage: ModelBudgetUsage
    budget_limits: ModelBudgetLimits
    task_priority: EnumTaskPriority


__all__ = ["ModelBudgetPolicyRequest"]
