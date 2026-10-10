# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing strict plan compute: the order of a landing tick's merges and branch updates on a strict base.

``HandlerPrLandingStrictPlan.handle(ModelLandingStrictPlanRequest) -> ModelLandingStrictPlanResult``
(definition-B). On a base that requires branches up to date it ranks the tick's merge and update-branch
actions, holds them behind a declared cause fix, defers the surplus updates, and derives the strict-slot
view the next tick remembers.
"""

from omnimarket.nodes.node_pr_landing_strict_plan_compute.handlers.handler_pr_landing_strict_plan import (
    HandlerPrLandingStrictPlan,
    plan_strict_order,
)


class NodePrLandingStrictPlanCompute(HandlerPrLandingStrictPlan):
    """ONEX entry-point wrapper for HandlerPrLandingStrictPlan."""


__all__ = [
    "HandlerPrLandingStrictPlan",
    "NodePrLandingStrictPlanCompute",
    "plan_strict_order",
]
