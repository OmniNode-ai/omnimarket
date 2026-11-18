# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan review ladder compute: the next move of a plan's review ladder.

``HandlerPlanReviewLadder.handle(ModelPlanReviewLadderRequest) ->
ModelPlanReviewLadderResult`` (definition-B). From the plan's first-slice
verdict and its review history it decides the next move: whole-plan rounds
stop once no blocking finding is open, then the first slice alone is reviewed
until a round is clean, then each later rung when it is next.
"""

from omnimarket.nodes.node_plan_review_ladder_compute.handlers.handler_plan_review_ladder import (
    HandlerPlanReviewLadder,
    check_plan_review_ladder,
)


class NodePlanReviewLadderCompute(HandlerPlanReviewLadder):
    """ONEX entry-point wrapper for HandlerPlanReviewLadder."""


__all__ = [
    "HandlerPlanReviewLadder",
    "NodePlanReviewLadderCompute",
    "check_plan_review_ladder",
]
