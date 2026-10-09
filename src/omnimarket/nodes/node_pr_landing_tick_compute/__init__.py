# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing tick compute: one landing-controller tick with free worker slots shared across repositories.

``HandlerPrLandingTick.handle(ModelLandingFacts) -> ModelLandingDecision`` (definition-B). It is the
decision of ``node_pr_landing_decision_compute`` and the per-repository fair share the live controller
applies on top of it, in one pure node.
"""

from omnimarket.nodes.node_pr_landing_tick_compute.handlers.handler_pr_landing_tick import (
    HandlerPrLandingTick,
    decide_landing_fair_share,
)


class NodePrLandingTickCompute(HandlerPrLandingTick):
    """ONEX entry-point wrapper for HandlerPrLandingTick."""


__all__ = [
    "HandlerPrLandingTick",
    "NodePrLandingTickCompute",
    "decide_landing_fair_share",
]
