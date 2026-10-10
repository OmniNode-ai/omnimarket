# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing class plan compute: the stale-summary and retarget actions of one landing tick, inside the quota cap.

``HandlerPrLandingClassPlan.handle(ModelLandingClassPlanRequest) -> ModelLandingClassPlanResult``
(definition-B). It plans which class actions the tick performs, defers the rest to the next tick, and
derives the sidecar memories the performed actions leave.
"""

from omnimarket.nodes.node_pr_landing_class_plan_compute.handlers.handler_pr_landing_class_plan import (
    HandlerPrLandingClassPlan,
    plan_landing_classes,
)


class NodePrLandingClassPlanCompute(HandlerPrLandingClassPlan):
    """ONEX entry-point wrapper for HandlerPrLandingClassPlan."""


__all__ = [
    "HandlerPrLandingClassPlan",
    "NodePrLandingClassPlanCompute",
    "plan_landing_classes",
]
