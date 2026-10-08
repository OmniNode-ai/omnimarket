# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hourly Linear-comments sweep decision compute node (OMN-20680)."""

from omnimarket.nodes.node_linear_comments_decision_compute.handlers.handler_linear_comments_decision import (
    HandlerLinearCommentsDecision,
)


class NodeLinearCommentsDecisionCompute(HandlerLinearCommentsDecision):
    """ONEX entrypoint for the sweep's deterministic decisions."""


__all__ = ["HandlerLinearCommentsDecision", "NodeLinearCommentsDecisionCompute"]
