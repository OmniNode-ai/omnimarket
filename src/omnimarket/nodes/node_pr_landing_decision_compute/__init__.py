# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing decision compute: one landing-controller tick as a pure function.

``HandlerPrLandingDecision.handle(ModelLandingFacts) -> ModelLandingDecision``
(definition-B). The rule set is the verified ``LandingController.tla`` model's
controller actions; the fixed worker briefs are typed models it emits. It has
no bus surface: the landing controller calls it in process.
"""

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    HandlerLandingJoinJ8,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    HandlerPrLandingDecision,
    decide_landing,
)


class NodePrLandingDecisionCompute(HandlerPrLandingDecision):
    """ONEX entry-point wrapper for HandlerPrLandingDecision."""


__all__ = [
    "HandlerLandingJoinJ8",
    "HandlerPrLandingDecision",
    "NodePrLandingDecisionCompute",
    "decide_landing",
]
